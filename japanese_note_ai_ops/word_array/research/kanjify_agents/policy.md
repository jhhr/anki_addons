# Kanjification policy

Policy version: 0.19

The one written policy for kanjifying a furigana sentence: which kana words are rewritten in
kanji, with which kanji, and how the result is written. Every kanjify agent reads it (the golden
set's word and sentence agents, the label-fix agents); it is the only place it is written. Each
rule has an id (`KANJI-3`, `SPLIT-よる`) that an agent cites for the choice it made. Examples
here are short generic phrases; examples from real sentences belong in the word decisions.

Rules marked **pending Qn** wait for the user's answer. A use that turns on one is written as
the rule's draft says and named with the question (see "Pending questions" at the end).

## BASIC: the test

Kanjify a word when it carries a meaning of its own; leave it in kana when it only does grammar.
Whether the word conjugates is not the test. When nothing below settles a use, this test does,
together with SOURCE and SPELL.

## SOURCE: does the word have a kanji spelling at all

- SOURCE-1 A kana word is kanjified only with a spelling a dictionary gives for that word, in
  that reading and part of speech. Enough: JMdict (jisho.org), where rarely used forms, ateji
  and irregular (iK) forms count (此れ, 一寸 for ちょっと, 泥々 for どろどろ: JMdict still lists
  them as the word's spelling) but outdated and search-only forms don't, and so does a spelling
  a sense note gives ("occ. written as 嗚呼" under ああ); the headword of a Japanese dictionary
  (大辞林, 大辞泉, 広辞苑, 明鏡, 新明解, 日本国語大辞典: the local MDX files, or weblio / kotobank
  pages of them), also where JMdict marks the same form search-only (いける as 行ける);
  wiktionary (en or ja), even alone. A contracted reading JMdict lists under a spelling counts
  as that spelling's (どっか as 何処[どっ]か, like 物[もん]). Not enough: ateji quiz and trivia
  sites, a kanji borrowed
  from a synonym that is read differently (徐々 read そろそろ, 確 read ちゃん, 沢山 read たっぷり),
  or a kanji that fits the meaning but no source gives. Such made-up gikun are un-kanjified. A
  typo in the input's kana (ぜい for せい) has no spelling in that reading either and stays kana:
  the note is fixed instead, and the word is kanjified once it reads right.
- SOURCE-2 Lengthened and sound-spelled forms stay kana even when the plain form is kanjified:
  ああ is 嗚呼, but あー, あぁ, あーあ stay; まー, おーい, うーん too. So do colloquial sound
  changes: ねえ for ない (and すげえ, うるせえ), っぷり for ぶり, an adjective stem + っ (すごっ,
  寒っ), a Sino-Japanese word spelled by its sound in katakana (サイコー), and a verb or
  adjective whose own ending is contracted (分かんない, つまんない, 行かなきゃなんねえ,
  泣きなさんな, 何すんだ, すりゃ, 嬉しかねえ as うれしかねえ, よかない, and する's imperative
  せぇ). Where only the auxiliary after it is contracted, the word is kanjified and the
  auxiliary stays kana inside its span (`<k> 行[い]けねえ</k>`, `<k> 糞[くそ]</k>もねえ`), and so
  is a verb + ない adjective whose ない alone is contracted (`<k> 下[くだ]らねえ</k>`, where
  つまんない, whose verb is contracted, stays). A ～,
  a ー or a small vowel kana after a complete dictionary form is intonation, not a lengthened
  form: the word is kanjified and the mark stays outside its span (`<k> 本当[ホント]</k>～`,
  `<k> 痛[いた]い</k>～`, `<k> 等[ら]</k>ァ`). The noun もん stays 物[もん] (FMT-5).

## SPELL: which spelling

- SPELL-1 Homophones (different dictionary words with the same kana: 付く / 就く / 着く / 突く /
  吐く, 取る / 撮る / 採る, 寄る / 依る, 内 / 家 for うち, 置く / 於く for おく): the word
  this sentence means, each with its own spelling. Where JMdict gives a sense both inside a broad
  word and as a narrower word of its own (差す and 射す for light, 取れる and 穫れる for a
  harvest), the broad word's main spelling is used (日が差す, 米が取れる) unless SPLIT lists it:
  so しつけ for manners is 仕付け (not 躾), ゆうべ for last night is 夕べ (not 昨夜), and a light
  that comes on is 付く (not 点く).
- SPELL-2 One dictionary word with several spellings: its main spelling (JMdict's first kanji
  form that is not outdated or search-only) for every sense, unless SPLIT lists the
  word. So よい / いい / よく are 良 in every sense (not 善, 能, 好), ちょっと is 一寸 (not 鳥渡).
  A main spelling that keeps part of the word in kana is completed by KANJI-12.
- SPELL-3 A word already spelled in kanji in the source is never respelled, even with a kanji
  this policy would not choose. Its kana part is still kanjified (KANJI-12).
- SPELL-4 A spelling JMdict does not mark common (a rarely used form, an ateji, a gikun) whose
  usual reading is another word is skipped: take the next listed spelling that reads as this
  word, and when there is none the word stays kana. `kanjify_lookup.py sudachi` is the check:
  a spelling Sudachi reads as another word, a reader takes for that word too. So そして stays
  kana (而して and 然して are read しこうして), and so does まとも (正面 is read しょうめん, 真面 as a
  name); あやなす is 綾なす (彩なす is read 彩り + 成す); おばあさん is 御婆さん (祖母 is read
  そぼ); もし, もしも, ほぼ and つくづく stay (若し is taken for わかし, 略 for りゃく, 熟 for
  じゅく). Read through its own parts with the word's own kana, a spelling is not another word
  (透かさず, 憖っか, 其れ其れ are kept). Common means common in this word's own entry: 縁 is
  common as えん, so ゆかり stays kana. A common spelling that is also another word's (未だ,
  止める, 側, 家) is kept: the furigana says which, and Sudachi, given one word alone, only
  guesses. So is a spelling a KANJI or SPLIT rule names, in any of the words it spells (見たい,
  為る, 此方, 彼奴, 序で; 其方 for そなた too, 如何 for いかん too).

## SPLIT: the words whose kanji follow the sense

Only these single dictionary words change kanji by sense. An agent that thinks another word
needs a split asks (a policy question); it never splits on its own.

- SPLIT-ある 在る when something is located somewhere, the place figurative too: a state,
  position or relation (机の上に在る, 危険な状態に在る, 密接な関係に在る), an abstract thing that
  lies in something (原因は其処に在る, 問題は量に在る), and a concrete thing that exists with no
  place named (昔、寺が在った). 有る for possession: a whole and its own parts or features (この
  部屋には窓が二つ有る, 足が四本有る), a person and a right, duty or quality (彼には責任が有る),
  and for events and abstract existence (自信が有る, 祭りが有る, 事が有る). The test for
  "Xに(は) Yが ある" is what X is: the whole Y belongs to, as its part, feature, contents or
  stock, takes 有る (町には運河が有る, 店には品物が有る, a body's organs with the body unsaid);
  a place that is not that whole takes 在る (家の周りに在る, 北に在る), and so does a time a
  period is placed at (千年前に在った). A count of things on hand is 有る (林檎が三つ有る). Set phrases where
  ある no longer means "exist" or "have" stay kana: だけあって, だけのことはある, とあっては,
  ったらありゃしない. The copula である stays kana (KANA-1).
- SPLIT-あう 遭う for meeting with a misfortune, an accident or a bad experience (事故に遭う,
  酷い目に遭う); 会う for every other meeting. 合う is a different word.
- SPLIT-あやしい 妖しい for mysterious, bewitching or alluring (妖しい美しさ); 怪しい for
  suspicious, doubtful, unreliable, strange or eerie, ominous, and every other sense (怪しい男,
  雲行きが怪しい).
- SPLIT-うまい 美味い for taste (美味い料理); 上手い for everything else, skill and things going
  well (上手い字, 上手く行く).
- SPLIT-おさえる 押さえる for pressing or holding down, securing (手で押さえる, 要点を押さえる);
  抑える for restraining or suppressing (怒りを抑える, 物価を抑える).
- SPLIT-かける 懸ける for staking or devoting something (命を懸ける, 名誉を懸けた戦い); 掛ける for
  every other sense of the word. 賭ける, a word of its own (SPELL-1), is only a wager (金を賭ける).
- SPLIT-きく 利く for a faculty, a function or working, and for speaking (口を利く, 気が利く,
  目が利く, 無理が利く); 効く for an effect (薬が効く, 宣伝が効く), a seasoning's in food too
  (わさびが効く, 塩の効いた味). 聞く / 聴く are other words.
- SPLIT-ただ 但 for the conjunction "but, however" (但、条件が有る); 只 for "only, just"
  (KANJI-10).
- SPLIT-つかまる 掴まる for holding on or clinging (手すりに掴まる); 捕まる for being caught or
  held, and for getting a taxi (犯人が捕まる, 車が捕まる).
- SPLIT-とる 採る for adopting or choosing (措置を採る, 方針を採る, 決を採る) and for gathering
  or extracting (山菜を採る, 油を採る); 摂る for taking in food, drink or nutrients (栄養を摂る);
  取る for everything else: an attitude or an action (態度を取る, 行動を取る), stealing (財布を
  取られる), sleep (睡眠を取る). One とる with objects of both kinds is 取る (食事と休息を取る).
  撮る and 執る are other words (SPELL-1).
- SPLIT-はじめ 初め for the first time or the beginning of a period (初めて, 年の初め); 始め for
  starting something (仕事を始める, 始めに述べた).
- SPLIT-もの 物 for a thing, 者 for a person, also as もん (物[もん], 馬鹿者[バカもん]).
- SPLIT-よる 由る for a means, method, process or mechanism: what an action is done by or with,
  and how an outcome comes about (手作業に由る, 機械に由る翻訳, 発酵に由って作られる, 使う事に
  由って鋭く成る); 因る for a cause that is an event, a condition or a natural force nobody does
  (事故に因る故障, 病気に因る欠席, 地震に因る被害, 重力に因って落ちる, 摩擦に因る磨耗), and for
  an event or act people did whose outcome was not what it was done for (戦争に因り記録が失われた,
  人為的な活動に因って), an event the outcome came about through too, even one the outcome is
  part of (戦争に因って国が分かれた), and for a thing that brought it about without being used for
  it, even
  in a passive (蚊の媒介に因る感染, 教えに因って変わった): a means is 由る only when it is used for
  the outcome, and a doer (依る) does the verb's action itself; the clause-final
  によって "because" is 因る too; 依る for "depending on", "according to", "based on" (人に依る,
  予報に依ると, 法律に依る, a person's favour or will as the ground: 御厚意に依る), and for the
  doer of an action, person or thing, in a passive or before an action noun (彼に依って書かれた,
  市民に依る運動, 細胞に依って作られる). The doer is
  who or what acts; 由る is only how it acts. 拠る is not used: its own sense, taking something
  as grounds or a source (法律に拠る, 資料に拠る), goes with 依る, since in real sentences it
  can't be told apart from "depending on" consistently.
- SPLIT-わたる 亘る for extending over or ranging (多岐に亘る, 長期に亘る); 渡る for crossing and
  every other sense.

## KANA: leave in kana (un-kanjify if a label kanjified it)

- KANA-1 The copula: だ, です, and である in all forms (であった, であり, であって, であれば,
  であろう), the polite でございます / でござる / でおじゃる, and the polite negative with では
  dropped (御存じありません). It is the copula, not で + 有る. The classical
  copula なり (〜なり, 〜なるべし) too, not 也, and its negative ならぬ / ならない after a noun
  (他ならない, 他ならぬ, 並々ならぬ): "is not other than", not 成る.
- KANA-2 ない as the negative auxiliary: 食べない, ではない / じゃない / ではなかった after nouns
  and na-adjectives, also with も or しか after the copula で (嘘でもない, 冗談でしかない),
  くない / くなかった after i-adjectives. Not 無い in kana, but grammar.
- KANA-3 て-form helper verbs in all their forms: ている, てある, てみる, てみせる, てくる, ていく,
  てくれる, てしまう, ておく, てのける, てもらう, ていただく, てあげる, てやる, ておる,
  ていらっしゃる, てまいる, and their contractions (てる, とく, ちゃう, ちまう, とる = ておる); also
  ないでいる, the ている helper after ない, and after ず (ずにはいられない, ないではいられない).
  Alone the same verbs keep their meaning and are kanjified (見る, 来る, 置く, 貰う, 呉れる, 遣る,
  仕舞う, 頂く): 見てみる, not 見て見る. The exceptions: くださる, ごらん and ちょうだい are
  kanjified after て too (KANJI-15); and やってくる, やっていく and でていく are one dictionary word
  each, written whole, in the senses named here only (遣って来る "turn up, come along",
  遣って行く "get by, get along", 出て行く "leave"). Where やる keeps its own object and くる /
  いく only adds time ("have done up to now", "keep doing"), it is 遣る + the helper
  (`今まで<k> 遣[や]ってきた</k>`). The list is closed: ついていく and ついてくる keep the helper
  (付いていく, 付いてきた). てやる is the helper; it is 遣る only where やる is an act of its own
  the context shows ("do, play"). くる after と is the verb, not a helper (と来ている, と来たら).
- KANA-4 て-form patterns: てほしい, てもいい / てもよい / てもよろしい and their negatives (てもよくない), ていい / てよろしい,
  ないでいい / ないでもいい / ないでもよろしい, てはいけない, てはならない, てならない / でならない ("can't help, unbearably"), てもかまわない,
  てはだめ; and a noun or na-adjective + でもいい (明日でもいい), the same pattern as てもいい. でいい without も
  stays 良 (これで良い), and so do ばいい (advice: すりゃ<k> 良[い]い</k>) and てよかった "glad that" (来て良かった),
  which are not the permission pattern; the permission's own past stays kana (てくれてよかったのに,
  "you could have just"). てはいけない and てはならない are the prohibition after a verb's or
  adjective's て-form; the obligation なくては / なくちゃ + いけない is 行けない, like なくては成らない (KANJI-4): `<k>
  為[し]なくて</k>は<k> 行[い]けない</k>`. Alone the words are kanjified (欲しい, 良い, 行けない, 成らない, 構わない, 駄目).
- KANA-5 する that only does grammar, holding no "do" or "make": として "as, in the role of" (教師として)
  and "not even one" before a negative (一日として無い); にしては "for a, considering"; にして after an age, a
  time or a moment, and in AにしてB; からして; にしたところで. Also the set adverbs and conjunctions and the
  suppositions built with する: もしかしたら, もしかして, ひょっとして, なんとかして, どうにかして, 要するに, ともすると, ややもすれば,
  ちょっとした, すると (the conjunction), そうして "and then", そしたら, それにしても, いずれにしても, としたら, とすれば, とすると, としても,
  だとしても, and a clause + として "supposing" or "leaving aside" (仮に本当だとして, それは措くとして). Only
  their する stays kana: a word in them that is kanjified on its own still is (其れにしても, 何れにしても,
  何とかして, 一寸した). An adverb or
  noun + と + する describing a state is 為る (平然と為て, 依然と為て, 断固と為て, even where JMdict lists the whole
  as an adverb: KANJI-3), and どうして is 如何為て (KANJI-6): the user's choice. So is とする with a
  を-object of its own, "take or regard A as B", in every form (AをBと為て, AをBと為ると, AをBに為て, AをBと為る,
  AをBと為た), and the finite "regard as" with or without one (と為れる, と為ている): KANJI-3.
  A を-object that belongs to the verb after として is not its own: in AをBとして認める, として
  only names A's role and stays kana. With no later verb that takes A, A is とする's own
  object (AをBと為て…).
- KANA-6 あげる meaning "to give": kept in kana to set it apart from 上げる / 揚げる / 挙げる. An
  exception to BASIC.
- KANA-7 そんな, こんな, あんな, どんな (and そんなに etc.).
- KANA-8 なんか as a filler or particle that could be dropped (今日なんか暑い); その and あの as
  a hesitation filler (その、何と言うか); もう as an exclamation (もう！, もう、やめてよ); もっと.
- KANA-9 Auxiliaries: れる / られる, せる / させる, ます, た, たい, らしい, ようだ's だ, ぬ, まい, and
  the そう of appearance and hearsay (降りそう, 来るそうだ): they inflect or join and add no meaning a
  kanji could carry. A kanjified one is a slip (れる written as 様). The auxiliaries that have a
  dictionary spelling and a meaning of their own are kanjified instead (KANJI-17).
- KANA-10 Particles (は, が, を, に, で, と, も, の, へ, や, か, ね, よ, しか, さえ, こそ...), the
  literary のみ too (not 耳, 已 or 許).
- KANA-11 Loanwords (gairaigo) stay katakana unless KANJI-16 kanjifies them.
- KANA-12 Names of people, places, works and brands, however they are written, and a title a
  work gives a character, with any さま it includes. Events are not among them: an event's kana
  is kanjified like a common noun (夏祭り), a place name in it stays. Nor is an honorific after
  a name, real or fictional: さま is 様 (SOURCE-1), くん 君 (KANJI-13); さん and ちゃん have no
  kanji. Names of plants, animals and other taxa are common nouns, not names (KANJI-14).
- KANA-13 そう and ああ as adverbs are always kana, the demonstrative too (そう言う, そうする,
  そうですね, ああ言う): 然う only spells the demonstrative, and telling it from the そう of
  appearance or hearsay is a call not worth making in every sentence; ああ has no spelling that
  fits either. 言う and 為る after them are still kanjified: そう<k> 言[い]う</k>. Their sibling
  こう is kanjified (KANJI-6): the user's choice, since 斯う fits it in every use.
- KANA-14 Kana that is mentioned, not used: a reading gloss (漢字（かんじ）), a word discussed as
  a kana form (「ついたち」とは), and its mirror, a kana word glossed right after by its own
  kanji in parentheses (ツバキ（椿）). Kanjifying it would erase what the sentence says. Only
  the mentioned word stays kana: the rest of a parenthetical is used and kanjified
  (（何方も、はし）), and so is the same word where the sentence uses it with no gloss. A word
  quoted for its meaning, or a quoted chant, is used, not mentioned: 「済みません」の意味,
  桑原桑原と唱えた.

## KANJI: kanjify

- KANJI-1 無い as a word of its own, in every form (なかった, なくて, なく): after a noun and は, が,
  も or しか (事は無かった, 一膳しか無い, 必要無い, 見た事無い), and in set phrases, also after
  kanji (絶え間無く) and where the phrase's own dictionary entry writes ない in kana (差し支え無い,
  余儀無く, 幾度と無く), or only as a search-only form (呆気無い, 極まり無い), and after 事 in a
  double negative (言えない事も無い, 行かない事は無い): the test is that ない means "there is
  none". Where it is a negative instead it stays kana: the copula's ない
  after a noun, では / じゃ / でも / でしか + ない (KANA-2), and である + なし ("whether or not
  one is"), also in a phrase with an entry of
  its own (過言ではない, 物の数ではない, 他でもない) and clipped (半端ない); an adjective's or
  べし's く-form + も + ない (欲しくもない, 可くもない); ったらない and と言ったらない
  ("indescribably"); とんでもない. The set phrases 満更でも無い, 何でも無い and 碌でも無い stay 無.
- KANJI-2 居る and 行く as verbs of their own (家に居る, あっちに行く), おる too (居る, humble),
  and いる after a noun or na-adjective + で (無事で居る, 平気で居る); ある as a verb of its own,
  有る or 在る by SPLIT-ある.
- KANJI-3 する is 為る everywhere, suru-verbs included: in every form and whatever follows
  (勉強為る, 為た, 為よう, 為て, 為ない, 為ず, 為ぬ, passive and causative 為れる / 為せる,
  為なさい), before a て-helper or ください (約束為てみる, 約束為て下さい), after a noun already in
  kanji (関為て, 支給為れる), one-kanji verbs included (愛為る, 化為る, 達為る), also those JMdict
  lists as -す verbs of their own (愛す and 博す as 愛為 and 博為, `処[しょ]<k> 為[さ]れる</k>`), after お / ご +
  noun (御願い為る), after kana words and onomatopoeia (にこにこ為る), after a volitional form
  ("try to": 立ち上がろうと為た) and after an adverb or noun + と (平然と為て). する's stem in a
  compound verb too, before JMdict's order of fuller forms (KANJI-12): 為直す, 為合う, 為過ぎる,
  為出す "begin to do". The する of KANA-5 stays kana, and so does the じる / ずる of a
  one-kanji verb (論じる, 減ずる): it is the verb's own ending, and 為 reads neither. どうして
  is 如何為て; どうしよう is 如何 + 為よう. But どうしようもない is 如何 + 仕様 + も + 無い: 仕様 is
  the noun "way, means" (仕様が無い).
- KANJI-4 なる is 成る in every use: ようになる, ことになる, そうになる, くなる, となる,
  なければならない / なくてはならない (探らなければ成らない), いかなくなる (行かなく成る). Only the
  て-patterns てはならない and てならない stay kana (KANA-4), and the copula's ならない / ならぬ
  after a noun (他ならない, KANA-1). But 実が生る (fruit grows) is 生る, a different word.
- KANJI-5 The formal nouns in every use, grammatical ones included: 事 (こと), 物 (もの, もん:
  SPLIT-もの), 為 (ため), 様 (よう, also ような, ように, ようだ), 所 (ところ), and every other
  formal noun with a dictionary kanji: 訳 (わけ), 筈 (はず), 積もり (つもり), 内 (うち), 儘 (まま),
  癖 (くせ), 方 (ほう), 通り (とおり), 所為 (せい), 御蔭 (おかげ), 程 (ほど), 許り (ばかり). So are
  the conjunctions and sentence endings made from them: 所が, 所で, 物の, 事に, 事だ, and a
  sentence-final もの / もん (嫌なんだ物[もん]).
- KANJI-6 The demonstratives: 此の / 其の / 彼の / 何の, 此れ / 其れ / 彼れ / 何れ, 此処 / 其処 /
  彼処 / 何処, 此方 / 其方 / 彼方 / 何方, the adverbs 斯う (こう, also 斯う言う, 斯う為て) and 如何
  (どう, also in どうも, どうして, どうにも), and いう after these and after そう / ああ as 言う
  (如何言う, そう言う). そう and ああ themselves stay kana (KANA-13). どうぞ is not どう + ぞ. A
  conjunction made of words kanjified here is written by its parts (其れで居て).
- KANJI-7 Particle-like words and set phrases with a kanji spelling: まで 迄, だけ 丈, くらい /
  ぐらい 位, ばかり 許り, ほど 程, ながら 乍ら, まま 儘, など 等, ら 等 (plural: 彼等, 其奴等),
  たち 達, とても 迚も, について に就いて, につき に就き ("per", "because of"), にかけて に掛けて
  (から〜に掛けて), にとって に取って, という と言う, みたい 見たい, いつ 何時, まるで 丸で, and
  the counter か / カ as 箇 (３箇月, ２箇所; ヶ and ヵ stay). For example 此れ丈は, 何れ位,
  気違い見たいに. The ら / いら of そこら, ここいら ("around there", "about this point") is not
  the plural and stays kana: 其処ら, 此処いら. A word with a JMdict entry of its own is written
  as that entry, by KANJI-12, not particle by particle: 出来る丈 and 成る可く (listed fuller
  forms), but 何時までも (何時迄も is search-only), and ピンからキリまで, which has no kanji at all.
  A verb in such a phrase keeps its own spelling (見掛けに依らず, 依らない).
- KANJI-8 The honorific prefix お / ご as 御 (御茶, 御願い, 御前), also where JMdict gives the word
  no 御 form or only a search-only one (御喋り, 御化け, 御握り, 御姉ちゃん): Sudachi splits the
  prefix off as a word of its own, and the rest follows its own rules. The input's own kana お
  before a kanji group is made 御 too (お 前[まえ] -> `<k> 御[お]</k> 前[まえ]`). A name's お
  stays (KANA-12), and so does the お of a kana-only word Sudachi doesn't split, which is part
  of the word (おちゃらける, おなら).
- KANJI-9 いい / よい / よく as 良 (SPELL-2), except いい加減 as 好い加減 (a word of its own). The
  exception list is closed: いい年 is 良い年.
- KANJI-10 ただ and たった meaning "only, just" as 只 (只今, 只一回); the conjunction ただ is 但
  (SPLIT-ただ).
- KANJI-11 Suffixes after a verb stem: やすい 易い, にくい / がたい 難い (付け難い), づらい 辛い,
  すぎる 過ぎる, かねる 兼ねる, なおす 直す (to redo), あう 合う (each other). After a mimetic or
  sound word in kana, つく is 付く (ピリ付く, ごちゃ付く, オラ付く), like にこにこ為る (KANJI-3); a
  stem already in kanji keeps its word's own spelling (愚図つく).
- KANJI-12 The kana part of a word partly written in kanji is kanjified when that part has a
  kanji spelling, also when JMdict lists the mixed form first: 引っかける -> 引っ掛ける, 近づく -> 近付く, やり方
  -> 遣り方, ため息 -> 溜め息, 子ども -> 子供, そのまま -> 其の儘; a kana word whose main spelling is mixed is
  completed the same way (やりとり -> 遣り取り). A rarely used (rK) or irregular (iK) form counts; a
  search-only (sK) one doesn't, and when that is the only fuller form the word keeps the main
  spelling. A fuller form must be one a dictionary lists for the whole word (痩せこける stays), and
  where the input already writes part of the word in kanji, its added kanji must read the kana
  part on its own: an ateji or jukujikun that only reads as a whole leaves the word as the input
  has it (田[た]んぼ, not 田圃). A kanji reads the part on its own when a kanji dictionary lists
  that reading for it, or the reading it is a voiced form of (付 for づ), name readings not
  counted: さざ 波 stays, since neither 小 nor 細 is listed as さざ. Among several fuller
  forms, take one that keeps the kanji the source
  has (SPELL-3), then one whose added kanji read the kana part (ほほ 笑[え]む -> 頬笑む, not 微笑む) and
  spell the kana part's own word (擂り下ろす, since すり is 擦る; not 摺り下ろす, since 摺 spells 刷る), then the
  one that keeps the kana part's own okurigana and main spelling as a word of its own (溜め息, not
  溜息; 飛び掛かる, not 飛び掛る; 小綺麗, not 小奇麗), then JMdict's order. A main spelling whose kana part is a
  word of its own (豚カツ, じゃが芋) is used only as a listed form, not with that part in the input's
  script: とんかつ and ジャガイモ stay kana (豚かつ and ジャガ芋 are search-only). Okurigana, unlike such a
  part, keeps the input's script (増[マ]シ, 嵌[ハ]マる). A lexicalized verb + ない adjective keeps the
  verb's okurigana (詰まらない, 詰まらぬ).
- KANJI-13 Also: なに / なん 何 (なんか / なんて as 何か when it means "something": なんか食べたい),
  いや 否, ああ 嗚呼 (the exclamation), また 又, 遣る (やる as a verb of its own), 君, 時, 方, 序で,
  御蔭 (おかげ, also 御蔭様で), 済む, 彼奴 / 此奴 / 其奴, 出来る, 振り, 貴方 (あなた), 尻餅を突く,
  余 (よ, "I"); おじ(さん) as 小父 / 叔父 / 伯父 by meaning, 小父 when neither the sentence nor
  its translation gives a sign of a relative ("uncle", "aunt" there is one); おば(さん) as an
  aunt is 叔母 in every use, and 小母 (an unrelated woman) also when the sentence gives no sign
  of a relative, as おじ; ばあ is 婆 in every compound
  (御婆さん, 御婆様, 御婆ちゃん, 婆ちゃん); いとこ is 従兄弟, or 従姉妹 when the sentence shows a
  female cousin (or only female ones), never the age-marked 従兄 / 従弟 / 従姉 / 従妹; わん
  (御椀) is 椀 unless the sentence says the bowl is ceramic or porcelain (碗): the material is
  not guessed from what is in it.
- KANJI-14 Native Japanese and Sino-Japanese words written in katakana are kanjified with the
  katakana kept in the furigana: 林檎[リンゴ], 塵[ゴミ], 奴[ヤツ], 駄目[ダメ], 馬鹿[バカ], 不味[マズ]い; also those
  JMdict marks as usually written in katakana (人[ヒト], 増[マ]シ, 落[オ]チ), and those whose katakana
  reading JMdict marks as having no kanji, which only records how the katakana is written
  (台詞[セリフ], 雑魚[ザコ], 判子[ハンコ], 鴨[カモ]る). The katakana okurigana of a verb stays after the group
  (嵌[ハ]マる, 持[モ]テる). A main spelling that mixes kana and kanji is completed by KANJI-12:
  玉葱[タマネギ], 薩摩芋[サツマイモ]; and a katakana part of a word the input partly writes in
  kanji is kanjified by the same rules, its okurigana in katakana (思うツボ -> 思う<k> 壺[ツボ]</k>,
  ハレ 着 -> <k> 晴[ハ]レ</k> 着). Names of plants, animals and other taxa, which biology writes
  in katakana, are included (キク 科 -> <k> 菊[キク]</k> 科): they are common nouns (KANA-12),
  kanjified where SOURCE and SPELL-4 give them a spelling. A katakana word the input wrote
  inside a kanji word's furigana group is broken furigana, not a word to kanjify (FMT-7).
- KANJI-15 Honorific verbs are kanjified wherever they are not a て-helper (KANA-3), after a
  verb stem too: 為さる (なさる) in every use (勉強為さる, 如何為さいました, 寝為さい: 寝[ね]<k>
  為[な]さい</k>, 御免為さい), except right after する's stem し, where なさい stays kana (為なさい,
  not 為為さい), and contracted なさんな (SOURCE-2); the あれ of 御 + noun + あれ as 有る
  (御覧有れ); 下さる (御待ち下さい), 頂く (御見せ頂く), ござる as 御座る (有難う御座います,
  此方に御座います; でございます stays kana, KANA-1), and お〜になる / お〜する by KANJI-4 and
  KANJI-3 (御帰りに成る, 御持ち為る). くださる, ごらん and ちょうだい are kanjified after て as well,
  in every form (教えて下さい, 為て下さい: `<k> 為[し]て 下[くだ]さい</k>`, 遣って御覧, 見せて頂戴):
  the user's exceptions to KANA-3; ていただく, てくれる, てもらう stay kana.
- KANJI-16 A loanword (gairaigo) is kanjified, the katakana kept in the reading, only when both
  hold: JMdict gives it a kanji spelling not marked rarely used or search-only, and Sudachi
  reads that spelling as the loanword (`kanjify_lookup.py sudachi`). So 珈琲[コーヒー], 煙草[タバコ],
  麦酒[ビール], 頁[ページ], 倶楽部[クラブ], 刷子[ブラシ], 歌留多[カルタ], 煙管[キセル]; but not 洋灯 (Sudachi reads ようとう), and
  none JMdict marks rare (米 for メートル, 瓦斯, 硝子, 洋袴, 釦, 弗, 混凝土). Mind homophones (SPELL-1): the
  kanji must spell this loanword, not a native word read the same: ボタン "button" is not 牡丹
  (peony), キス "kiss" not 鱚 (a fish), パイ "pie" not 牌; and another loanword read the same stays
  katakana: 倶楽部 is a club as a group or its house, not a nightclub. A compound loanword with an
  entry of its own and no kanji stays katakana whole (アイスコーヒー). Names of countries, places and
  people stay as written (KANA-12), whatever kanji they once had (亜米利加, 印度). The same loanword
  is kanjified in every sentence or in none: extract_words records a kanjified one under its
  kanji and a katakana one under its katakana, so a mix splits one word over two notes.
- KANJI-17 An auxiliary with a dictionary spelling and a meaning of its own: べし 可し (in every
  form: 可き, 可く, 可からず), ごとし 如し (如き, 如く), よう 様 (KANJI-5). And みたい 見たい
  (KANJI-7): no dictionary gives it, and Sudachi reads it as 見る + たい; it is the user's
  exception to SOURCE-1 and SPELL-4.

## FMT: how the result is written

- FMT-1 A kanjified word is wrapped in `<k>` tags with a space before its kanji and its reading
  in brackets: `これ` -> `<k> 此[こ]れ</k>`. The reading is exactly the kana the kanji replaces,
  okurigana and inflection follow in kana inside the tags: `まわってた` -> `<k> 回[まわ]ってた</k>`,
  `しかないの` -> `しか<k> 無[な]い</k>の`. Inflection includes the auxiliaries after a verb,
  らしい and the そう of appearance or hearsay too, with what inflects them up to the next particle
  (`<k> 食[た]べたい</k>`, `<k> 為[し]ました</k>`, `<k> 成[な]るらしい</k>`,
  `<k> 有[あ]りそうです</k>`), the verb's conjunctive and conditional endings (て, ても, ては, ば,
  たら, たり: `<k> 為[す]れば</k>`), a KANA-4 pattern after it (`<k> 為[し]てもいい</k>`), and
  ようだ's な / に / だ / です after 様 (`<k> 様[よう]な</k>`, `<k> 様[よう]です</k>`). A particle
  stays outside: the conditional と (`<k> 為[す]る</k>と`), a sentence-final よ or ぞ, a
  na-adjective's な or に (`<k> 些細[ささい]</k>な`), and the ん of のだ (`<k> 付[つ]いて</k>んだ`);
  the ん of a contracted てる before a sentence-final な is inflection (`<k> 成[な]ってん</k>な`).
  A particle that is part of a word's dictionary spelling stays in it (`<k> 割[わり]に</k>`,
  `<k> 事[こと]に</k>`). A helper after a kanjified particle-like word is outside its span
  (`<k> 為[し]て 許[ばか]り</k>いた`).
  A space the input has right before a word that becomes kanji is taken by the span's own
  leading space (`が まだ` -> `が<k> 未[ま]だ</k>`): the checks ignore whitespace.
- FMT-2 Each run of kanji gets one group for its word, as the collection's own furigana has it:
  `たくさん` -> `<k> 沢山[たくさん]</k>`, `ときどき` -> `<k> 時々[ときどき]</k>`,
  `とうもろこし` -> `<k> 玉蜀黍[とうもろこし]</k>`. Kanji with kana between them each get their
  own: `つきあい` -> `<k> 付[つ]き 合[あ]い</k>`, and a prefix is a word of its own, also before
  one kanji: `ごちそう` -> `<k> 御[ご] 馳走[ちそう]</k>`, `<k> 御[ご] 覧[らん]</k>`,
  `<k> 御[ご] 免[めん]</k>`.
- FMT-3 A `<k>` span covers a run of kanjified words and stops at a word already in kanji:
  `タンパク 質[しつ]` -> `<k> 蛋白[たんぱく]</k> 質[しつ]`. Kanjified words next to each other
  share one span, a space between their groups: `<k> 其[そ]れ 程[ほど]</k>`. Where KANJI-12
  adds kanji to a word the input partly writes in kanji, the span holds only the added part, in
  a group of its own beside the input's: `近[ちか]づく` -> `近[ちか]<k> 付[づ]く</k>`. A kana
  part the main spelling keeps stays outside the span (`もう<k> 直[す]ぐ</k>`).
- FMT-4 A kana helper after a kanjified verb goes inside that verb's span as okurigana:
  `してみます` -> `<k> 為[し]てみます</k>`. After a verb already in kanji the helper gets no tag:
  `行[い]ってみましょう` stays.
- FMT-5 Katakana is kept in the reading: `バカ` -> `<k> 馬鹿[バカ]</k>`; colloquial shortenings
  are kept: `もん` -> `<k> 物[もん]</k>`, `バカもん` -> `<k> 馬鹿者[バカもん]</k>`.
- FMT-6 Existing HTML tags stay where they are and outermost: `<b>これ見よがしに</b>` ->
  `<b><k> 此[こ]れ</k> 見[み]よがしに</b>`. Text inside `<i>` (a context sentence) is kanjified
  like the rest.
- FMT-7 Everything outside the new spans is unchanged: the words, their furigana, spaces (but
  FMT-1's), punctuation and tags. Turning every span back into the kana it reads must give the
  input, with no exception. Kana the input wrote inside a kanji word's group, the reading
  covering it (`ネコ科[ねこか]`, `カ国[かこく]`), is broken furigana: the note is fixed by
  splitting the group (`ネコ 科[か]`) and kanjified from there. A program lists it with that
  fix and holds the sentence back; a labeller leaves the group as it is.
- FMT-8 Numbers: furigana may be added to a number without `<k>` tags, and nothing else about it
  changes (`１つ` -> `１[ひと]つ`, `10分[ぷん]` -> `10分[じゅっぷん]`).

## Pending questions

None: every question put to the user so far is answered above. A use the policy does not settle
is still not the agent's to decide: a word agent asks, a sentence agent hands the word back.
