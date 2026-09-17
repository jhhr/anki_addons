# word_array

Generates the word arrays of the extracted-vocab redesign: a partition of a furigana sentence
into dictionary words, each `[raw_text, part_of_speech, dict_form, reading, match_data,
sub_words]`, with tags and punctuation as single-element arrays. Concatenating the top-level
`raw_text` values gives back the sentence (minus `<b>` tags), so any word can be wrapped in
`<b>` without inflection matching.

Every op writes the field through `match_flags.format_word_array`: a row per top-level word,
sub-words indented under their parent, `", "` between a word's own values. One line of JSON is
513 characters for the median sentence in the collection and 4160 for the longest, which is
neither readable in the note editor nor legible in a diff — the old word list field was stored
a row per word for the same reason. `decode_word_array` reads it back, and undoes what Anki's
editor makes of those rows (`<br>`, `&nbsp;`) when a field fails to parse as it stands.

Status: in use. `async_api_ops/extract_words.py` ("Extract words") generates the array for a
note's sentence and asks the proper noun model about it; the word matching judge then decides
which words are worth a note, and match_words_to_notes and clean_meaning read the array. The
collection was migrated on 2026-09-16.

## Dependencies

- **SudachiPy** is vendored through `requirements.in` like the other packages: ~1.5 MB per
  platform, in `lib/_platform/<tag>/`.
- **Sudachi's dictionary** and **JMdict** are downloaded on first use into `user_files/`
  (`resources.py`), since Anki keeps only that directory across add-on updates. The dictionary
  (`sudachidict_core`, a 72 MB wheel unpacking to ~200 MB) is fetched from PyPI at a pinned
  version and sha256 and only its `system.dic` kept; JMdict_e (11 MB) comes from EDRDG, and
  its lookup index is built once (~15 s) and pickled. `resources.missing()` lists what is
  needed and how big it is, so the op that first uses the generator can ask before calling
  `resources.ensure()` in a background task. Until then `generate()` raises
  `ResourcesMissing`.
- Licences: JMdict is the property of the EDRDG, used under its licence (CC BY-SA 4.0), which
  asks for acknowledgement; the Sudachi dictionary is Apache 2.0 (with UniDic's terms in its
  LEGAL file).

## How it works

`generator.generate(sentence)`, in stages:

1. **`text_map`** strips tags and furigana and remembers where everything was. Words inside
   `<k>` are reverted to their furigana reading first: `<k>` marks words that were kana before
   kanjify_sentence ran, and reading `遣[や]っ` as kanji gives 遣う (つかう), `為[さ]れ` gives
   なる, `為[す]る` before 際 gives ため. Tokenizing the original kana fixes most wrong lemmas.
2. **Sudachi** (`SplitMode.C`), keeping each long unit's `SplitMode.A` split for sub-words.
   `normalized_form` turns kana lemmas back into kanjified ones (する -> 為る, これ -> 此れ,
   くださる -> 下さる).
3. **Grouping** morphemes into words: a verb or adjective plus its inflection chain (助動詞,
   て/で/ば, auxiliary いる). しまう, やる, おく and other auxiliaries stay separate words, and so
   do the modal 助動詞 らしい, べき and まい (捌いとる + らしい, 帰る + べき); たい stays in the chain.
4. **Furigana groups are never split between top-level words**, which repairs tokenizer cuts
   like 八紘|一宇 and 業|者.
5. **Multi-word candidates**: n-grams of words that are JMdict entries, looked up as written,
   as tokenized (the `<k>` kana) and with the last word deinflected (様に成った -> 様に成る).
   When the dictionary form is another JMdict entry it is tried first (気を付けて -> 気を付ける,
   に依って -> に依る, に対して -> に対する), the as-written match kept for when it is no word
   there (に就いて); entrenched inflections stay as written: `ENTRENCHED_EXPRESSIONS` (そう言えば,
   と言われる, 主として, 面と向かって) and adverbial 〜無く (間も無く, 余儀無く).
6. **Structure** (below).
7. **Dictionary form and reading.** Readings come from the note's own furigana wherever it has
   it; inflected words take the JMdict reading of their lemma that agrees with the furigana stem.
   A multi-word unit matched with its last word deinflected takes its dictionary form and
   reading from its parts, in the note's spelling (様に成る / ようになる, not ようになる).
   An adjective's く- and な-forms are the adjective (大きく, <k>良く</k> as a 副詞, 大きな as a
   連体詞, 多く and 近く as nouns with a kanji), save the few adverbs of their own in `LEXICAL_KU_ADVERBS` (危うく, 全く): JMdict
   lists 大きく and 早く as adverbs too, so it can't tell them apart.
   A word Sudachi calls a suffix that JMdict has with its reading only as a word of its own is
   labelled as JMdict has it: 家[うち] after 一日中 a noun, 等[など] a particle, 沿い a verb. Inside
   a compound only the verb stems are relabelled; 官 of 警察官 stays a suffix, though JMdict's 官
   is a noun. Of 855 top-level suffixes in the collection export, 73 change.
   A verb stem Sudachi calls a suffix is its verb (付き -> 付く) unless JMdict has the stem, read
   as the note reads it, as a `suf` of its own: 振り[ぶり], 通し[どおし], 合い, 込み stay as written.
   A sub-word noun listed as its verb is labelled a verb too (買い of 買い物 -> 買う), so the
   judge reads it by the verb rules. A word of its own stays the noun when JMdict has it spelled
   and read so (動き, 周り, 嫌い, 積り: lexicalized, and the old lists kept the noun), and no stem
   is its verb when the verb can't be read so (黙り[だんまり]). Export: 884 stems as verbs, 11 top-level.
   Furigana the note put on a word's last kanji for the text before it too (空</b>域[くういき],
   ネット上[ねっとじょう]) gives the word only its own part (いき, じょう), when the rest reads the
   text before it.
   **One spelling per JMdict entry** (`canonical_form`, last): spellings of the entry read so
   with the same kanji, or kanji in place of kana, are one word spelled with the most kanji
   (私たち -> 私達, という -> と言う, まで -> 迄, お願いします -> 御願いします), ties going to the
   unmarked, then common, then first JMdict spelling (積り -> 積もり); outdated kanji collapse
   too (爲に -> 為に). Other kanji stay words of their own (聴く, 体 not 身体), as do kanji added
   to kana (高い, not the search-only 高価い), katakana (フランス), a kana word JMdict doesn't mark
   usually-kana (そうに, not 僧尼) and one with several kanji choices (よる: 依る/因る/拠る/由る,
   `research/canonical_forms.py` lists them). The JMdict index keeps each spelling's ke_inf/ke_pri,
   re_restr and uk for this. Export: 1636 words respelled, entries reached by more than one
   spelling 720 -> 444 (different kanji: 有る/在る, 聞く/聴く).

A sub-word that ends inside a furigana group gets its own share of the reading, split per kanji
by `kana_highlight`: `見下[みお]ろせた` -> ` 見[み]` + `下[お]ろせた`. A jukujikun group has no
per-kanji split, but it can still be split between two words whose own readings add up to it:
`為替相場[かわせそうば]` -> ` 為替[かわせ]` + `相場[そうば]`, each reading taken from the
sub-word's Sudachi and JMdict readings, the second allowed to be voiced by rendaku
(`土産物[みやげもの]`). A sub-word never comes out as bare kanji: when the readings don't add
up, the word simply keeps no sub-words, which is what stops a jukujikun word being split inside
(今日, 田舎者). A sub-word's reading drops the rendaku of the compound it came from when JMdict
has the plain reading (閏日 -> 日[び] -> ひ).

Where the note gives no furigana, Sudachi's reading is shared out the same way, since its short
units don't always add up to the long unit: 無人島 comes as 無人[むじん] + 島[むじんとう], so 島
takes its own Sudachi or JMdict reading (とう), and 一日中[いちにちじゅう] as 一 + 日中[にっちゅう],
which nothing reads, so it keeps no sub-words. A JMdict match must add up to JMdict's reading,
except a number before a counter (一本 is いっぽん). Only a word with no furigana at all loses
its sub-words this way: furigana on part of a word is mostly its whole reading put on one kanji
(`<b> 無人</b>島[むじんとう]`), and those sub-words stay, often already linked to notes. Over the
export this re-reads 9 sub-words (古代人 -> 人[じん]) and drops none, carrying the same links;
`research/sub_readings.py` counts what still doesn't add up.

## Structure

Which words exist follows concrete rules; whether a word is worth matching to a note is the
word matching judge's call (`match_data`), not the generator's. So the rules err towards more
words: nesting loses nothing, and the judge is what filters.

**Multi-word units.** Every JMdict match becomes a parent with its words as sub-words
(連れて行く, 様に成る, 先ずは, 何時まで, 足が竦む, 此れは), except where it is not a word of the
text at all:

- function words only (には, のだ, か+の read as 彼の);
- matched by its spelling where the note's furigana reads it as no reading of the entry:
  彼[かれ]の is not 彼の (あの), 今日[きょう]は not 今日は (こんにちは);
- found only through its kana, starting on a particle or a suffix, and not spelled that way in
  JMdict: は+幾つ read as はいくつ (背屈), を+持って as をもって (を以って), 大切さ+が as さが (性),
  木下さん+と as さんと (三都); 45 such suffix-led matches in the export. The text's kana must
  match one of the entry's spellings, with the same kanji or kanjified kana between them, so
  だけの事は有って still matches だけの事はある;
- found only through its kana where the text writes kanji, and JMdict spells the entry otherwise:
  `<k>成[な]ると</k>` read as 鳴門 (なると), 事に as 殊に. A kanji run of the text agrees with a
  spelling whose kanji it has (如何為て is 如何して). Of 1995 such matches in the export 519 were
  refused; links carried 76061 -> 76044, the losses being old lists that linked a homophone or a
  variant spelling (余りに, 其れら).

A match inside another nests in it (様に成る -> 様に + 成る, 様に -> 様 + に). Of two that cross,
the longer wins, then the one found by its kanji spelling, then the earlier (一つ over つの).
Runs of nouns that are neither a JMdict entry nor a Sudachi long unit stay separate words
(声 高々, 配役 ミス, 出展 拒否).

**Sub-words of one word**, from either

- Sudachi's short units of a long unit, always: 飛行機 -> 飛行 + 機, 生徒会 -> 生徒 + 会,
  私達 -> 私 + 達, 遂行能力 -> 遂行 + 能力, 幾つ -> 幾 + つ; or
- a 2-way JMdict decomposition of a word Sudachi has as one unit: 耳元 -> 耳 + 元,
  頭ごなし -> 頭 + ごなし, 正に -> 正 + に, 大空 -> 大 + 空. The split must fall where the
  furigana splits per kanji, each piece must be a JMdict entry with the reading the note gives
  it (the second may carry rendaku), and no piece may be a lone kanji read in on'yomi: those
  are mostly bound morphemes, and allowing them would split every on'yomi compound (最|近,
  言|語). Nor may the second piece be okurigana: kana ending a na-adjective (柔らか, 静か, 新た,
  見たい; 68 in the export) or an adverb other than a particle (悉く, 幾ら, 何しろ), though JMdict
  has らか, か and く as words. A noun's lone kana other than も is okurigana too (窪み, 夕べ,
  逆さ, 幾ら; 何時+も stays), and so is a longer tail of a noun that is a verb's ます-stem (味わい,
  温もり, 見かけ), while 赤+ちゃん, 口+コミ, 目+つき still split. A conjunction or interjection
  never splits off its kana (但し, 並びに; 済みません, 初めまして, 今日は), and a pronoun only に, も,
  か or a longer tail (其こ one word; 私+たち). `research/okurigana_decomp.py`.

An adjective stem with the na-adjective suffix after it is one na-adjective whether JMdict has it
or not: 儚げ like 寂しげ (儚い + げ), so 忌々しげに is not 忌々し + げに.

A furigana group cut by the tokenizer is one word when JMdict has the whole read that way
(八紘一宇, 業者; its sub-words then follow the rules above); otherwise its pieces are the words
(天|高く, 軽音|部), unless they are all lone on'yomi kanji, i.e. a name cut into characters
(里|樹).

## Numbers

A number's dictionary form is the Japanese numeral whatever the text writes (`numbers.py`):
1, １, `1[いち]` and 一 are all 一, and 1935 is 千九百三十五. The raw text keeps its own form, as
きったら does for 切る. Numbers are the text most often left without furigana, so the reading is
computed when the note gives none (三つ still reads みっつ, from JMdict). Lookups use the
numeral, so 1日 and １日 both find 一日.

## Names

Sudachi doesn't know fictional names and nicknames, so it cuts them into words it does know
(里|樹, ひま|りん) or tags a common one as a plain noun (山田). `names.py` finds them across a whole
corpus by what grammar puts around a name: an honorific after it (里樹さま), a nickname suffix
fused to it (ひまりん), or its opening quoted speech (「ひまりん、). One anchored use names every
mention read the same way. A dictionary word needs at least two anchors and most of its uses
anchored (娘さん doesn't make 娘 a name), and お-words and hiragana words never are names. A name
Sudachi tags 固有名詞 somewhere that is also a dictionary word (the surname 谷) is a name only where
Sudachi tags it so or an honorific follows: 深い谷, 玄関のベル stay nouns. Over the export that drops
13 mentions, 8 of them wrong names; the 5 lost real ones (凛！, 黒木の) are the proper noun op's.

`generator.build_name_lexicon(sentences)` builds the lexicon; `generate(sentence, names=lexicon)`
merges each name it finds into one `proper noun` with no sub-words, leaving the honorific a word
of its own. The add-on keeps the lexicon in `user_files/name_lexicon.json`, built by the browser
menu entry "Build name lexicon from selected notes" (select the whole collection) and read by the
migration op; without one, names stay cut up. Over the export: 115 names, links carried unchanged
(76087), 22 fewer array words. `research/name_lexicon.py` reports what the lexicon holds and misses;
`migrate_fit.py` builds one from its corpus unless given `--no-names`.

Without a lexicon, three rules within the sentence fix Sudachi's proper noun calls: katakana nouns
joined by ・ are one name when a part is a Sudachi proper noun or not in JMdict (ナツキ・スバル, not
テレビ・カメラ or a list of places); an out-of-vocabulary katakana noun of 3+ characters JMdict
doesn't have is a proper noun (フェザーン); and a Sudachi proper noun the furigana reads as another
JMdict entry takes that entry's label (亜人[あじん], not Sudachi's name つぐと; 日本[にほん] stays, one
entry with にっぽん). A compound JMdict labels a noun (江戸時代, 日本文学) stays a noun. Over the
export, old proper nouns labelled one 997→1084 of ~2170, links carried unchanged.

What rules and lexicon still miss, a language model catches: `proper_noun_llm.py` asks only which
proper nouns the sentence has (a JSON list), and `fix_array` makes every name that starts and ends on
top-level word boundaries one proper noun without sub-words (a name inside a word, 日本 of 日本語,
changes nothing). Op `async_api_ops/find_proper_nouns.py`, one request per note, browser entry "Find
proper nouns in word arrays", model `proper_nouns_model` (default gpt-5.6-luna, the most precise
below). It is its own step of the migration: build the name lexicon, migrate, find proper nouns,
then judge. "Extract words" makes the same call inline (`add_proper_nouns`, sharing the tokenizer
lock), so a freshly generated array has been through it already.
`research/proper_noun_eval.py` scores models on 300 export sentences with old proper nouns and 300
without: names precision/recall against the old lists (noisy: 彼女 is listed 16 times), and old proper
nouns labelled a top-level proper noun before and after the fix, 194 of 375 without it:

| Model | Names P / R | Top proper noun after | Names changed |
| --- | --- | --- | --- |
| gemini-3.1-flash-lite | 82.7% / 69.0% | 254 | 114 |
| gemini-3.5-flash-lite | 85.1% / 69.0% | 253 | 101 |
| gpt-5.6-luna | 87.6% / 66.0% | 249 | 84 |
| gpt-5.6-terra | 85.3% / 65.2% | 245 | 85 |
| claude-haiku-4-5 | 80.0% / 67.4% | 251 | 113 (日本語, 猫足 wrongly) |
| claude-sonnet-5 | 83.7% / 72.7% | 267 | 120 |

Most names given that the old lists lack are real (高松塚古墳, 奈良県, 正親町天皇). A name off word
boundaries is nearly always part of a word on purpose (スペイン語, 新宿駅, 奈良県明日香村). Where
the generator cut across a name, `generator.name_rest_word` lets `fix_array` split the word first:
when what is left is a particle (凛と -> 凛 + と), or a JMdict word and the cut word is neither a
JMdict entry nor a proper noun (少年京太郎 -> 少年 + 京太郎; 江戸時代, ドイツ語, 新宿駅 stay whole).
Sub-words the cut runs between keep their links. A JMdict common noun isn't refused for now:
JMdict labels 江戸時代, 警視庁 and names like ルーク, 根元 `n` too, so such a guard would drop about
40% of the fixes.

## Generating a note's array

`async_api_ops/extract_words.py` ("Extract words") is what gives a note its array: `generate()`
on the sentence with its `<i>` context stripped (`html_stripping.strip_context_sentences`, the
way the old op took it) and the collection's name lexicon, then the proper noun call on the
result. One `bulk_notes_op` request per note, one Sudachi tokenizer at a time
(`find_proper_nouns.generate_word_array`), and the first run asks about the dictionary downloads
before touching a note (`with_generator_resources`). The words come out unjudged, `[]`.

A note whose field already holds an array is skipped, so re-running over a selection is free; a
note still holding an old extract_words word list is skipped too, because generating over it
would throw away the note ids of its matched words - that is the migration's job to carry over.

### Regenerating over an array

"Regenerate words over the current array" is the same op with `overwrite=True`, for the note
whose sentence turned out to have a mistake in it: the field says ` 小[しょう] 枝[えだ]` and the
word is 小枝[こえだ], the furigana is corrected in the sentence field, and the array has to
follow. Hand-editing the rows the correction touches is tedious and easy to get wrong, and until
this op there was nothing else - the first guard refuses the note, and clearing the field would
throw away every judgement in it, not just the ones the correction is about.

Both steps above run again, on the whole corrected sentence: it can change how the words around
the correction are read, and a name can appear in what it changed. `merge.py` then merges the
result into what the field holds. Both arrays are this generator's output over nearly the same
sentence, so everything the correction did not reach comes back identical and a diff of the two
says exactly which rows it did reach: an unchanged row keeps the old element whole, `match_data`
and sub-words and all, and a changed one is taken from the new array. A row counts as unchanged
only if everything the generator decides about it is the same - raw text, part of speech,
dictionary form, reading, and the same again for each sub-word - so a word the correction made
the generator read differently is a changed word and the new reading wins.

Within a changed region `match_data` is carried over by dictionary form and reading, and then by
dictionary form alone, which is what recognises the word the old array misread (小枝 read
しょうえだ). Each step carries only where exactly one old and one new element of the region
still holds the key; two candidates is what `research/migrate.py` decided not to guess at, and
the op logs the note id of every link it could not carry. The second guard still stands: an old
extract_words word list is left alone either way, since the migration that could read one is
gone.

The words a regeneration brings in are unjudged like any others, so "Judge words matchability"
is what follows it and then "Match extracted words to notes". A regeneration that changes
nothing writes nothing, except that it tidies a field Anki's editor has turned into `<br>` and
`&nbsp;` - which is the state hand-correcting a note leaves it in.

The judge cannot be a step inside that op: `bulk_nested_notes_op` fixes every note's task count
before it starts, and the words only exist once the array has been generated. So it is a phase of
its own (`base_ops.OpPhase`, `run_op_phases`): "Extract words + Judge matchability" is the two ops
over one selection as one operation, and "Run all ops for new notes" ends with the same judge
phase. The prompt the old op asked the whole word list under is gone, and with it "Test extract
words prompt" and the extract-words fine-tuning export.

## match_data states

`match_data` is in one of five states (`match_flags.MatchState`, `match_state()`):

1. `[]` - unjudged: new from the generator, or migrated without a note id.
2. `["dontmatch"]` - judged not worth a note.
3. `["match"]` - judged worth a note, not matched yet.
4. `[note_id]` - migrated with a note id: judged already, lacks `match_quality`.
5. `[note_id, match_quality]` - fully matched.

Numbers other than 一-九, 十, 二十 and the multipliers start out `dontmatch`, and so does a word
built on one (二十八日, 十一時): numbers have been a steady source of junk notes. Everything
else is the word matching judge's call. The modes are `JUDGE_NEW` (state 1, the default),
`REJUDGE_MATCHED` (4, 5) and `REJUDGE_ALL` (2-5); re-judging can take a link away.

The judge (`judge.py`, op `async_api_ops/word_matching_judge.py`, one browser menu entry per
mode: "Judge words matchability", "Re-judge matched words", "Re-judge matched/judged words", model
`word_matching_judge_model`) asks about each word alone: its prompt has the rules for its group
(`POS_RULES`, `rule_group()`: the part of speech's group, split further wherever the array tells
cases apart - nouns into `noun-main` at the top level, `noun-phrase` inside a word made with a
particle (本当に, 羽目を外す) and `noun-sub` inside any other word, either verb of a two-verb
compound verb into `prefix-verb` / `suffix-verb`, affixes into `prefix`, `suffix` and `counter`,
and expressions of verbs only into
`verb-expression`, of noun + particle + verb into `collocation`), the sentence with it in `<b>` (`match_flags.iter_highlighted()`, built
from the array's own raw texts, so the note's sentence isn't needed), and the words it is part of
or made of, and returns `{"reason", "decision"}`. Particles and the copula are judged `dontmatch`
without a request. A note's requests run in parallel through `bulk_nested_notes_op`; the array is
saved when all are done, a word whose request failed left as it was. A field still holding an old
word list is skipped, and unlinked note ids are logged. It replaced a v1 judge that asked about a
whole sentence's words in one prompt.

`research/judge_eval.py` scores the judge against the hand-checked export, whose old lists count
as its ground truth: a word an old entry fits is `match`, one none fits `dontmatch`, particles and
the copula no entry fits are left unscored, and the judge is not scored on particles and the
copula at all. gemini-3.5-flash-lite, 384 sentences: 83.1%, picks 78.1% precise with 49.1% recall
(v1 had 74.1%, 50.5%, 45.0%); with 237 hand-judged sentences added, 621: 82.8%, 72.8%, 47.5%, the
645 hand-judged words 82.5%. Providers on the first 80 sentences (487 words): gemini-3.5-flash-lite
88.3% (P 81.1% R 58.3%); claude-haiku-4-5 over the API 84.4% (60.2%, 77.7%, 302 calls/min at 8
workers); `terminal-claude-haiku-4-5`, the same model through the `claude` CLI on the subscription,
85.8% (63.9%, 75.7%, 80 calls/min through its 16-process cap, 0 failures). Haiku picks far more
dontmatch than gemini. `build` and `hand_judge.py` generate with the export's name lexicon,
as the migration op does with the collection's. The labels are unsure for components of compounds, so more
hand-judged words are to come: `research/hand_judge.py` serves a page that offers words of the
migration export one at a time, from the rule groups ticked, with Match / Don't match buttons, and
writes `output/word_matching_judge_hand_labels.jsonl`. `judge_eval.py build` lays those over the
checked labels (a hand-judged sentence outside the checked export is asked about only its judged
words), and `run` scores them on a line of their own too. Export rows carry `nids`, every note with
the sentence (`hand_labels.read_export_nids`), and `research/anki_connect.py` reads and edits those
notes through AnkiConnect, for fixing a sentence from the hand judge's page: Open in Anki browses
the sentence's notes, Refetch note regenerates an edited sentence at the front of the queue, moves
its labels to the new text (dropping those whose word is gone) and rewrites its export and checked rows.
Clicking an outlined `<k>` span writes it back as kana to every note of the sentence
(`research/note_edits.py`: `<k> 此[こ]の</k>` → `この`) and goes on as Refetch does; Anki can't
undo that write, so Revert last edit puts the old field back.

`research/kanjify_eval.py` scores kanjify_sentence's prompt against the checked kanjified
sentences (the audit's rows): answers cleaned as the op cleans them (`clean_kanjified`), `<k>`
furigana groups placed by their kana and scored right / wrong kanji / missed / extra, by class
(policy, formal noun, 為る, 成る, 依る/因る, other). On the 463 audit-fixed labels (span P/R), prompt
before draft 1: claude-sonnet-5 92.4/77.2%, gpt-5.6-luna 89.3/79.7%, gemini-3.5-flash-lite 87.0/73.1%,
`terminal-claude-opus-5` 83.9/82.3%, `terminal-claude-haiku-4-5` 61.2/53.5%. Draft 1 (する after a
kanji noun, particle-like words 迄/丈/位/見たい…): sonnet-5 93.0/88.8%, luna 90.0/89.2%, gemini
88.4/83.8%. Draft 2 (loanwords stay katakana, 無い after は/しか, kana part of a kanji word, 斯う/然う/如何,
在る/有る, 良い/好い, 只): sonnet-5 94.4/91.6%, luna 90.9/90.9%, gemini 89.9/86.3%. Draft 3 (為る in every form and after と, 成る in なければならない/くなる, 儘, final
する/なる check): sonnet-5 94.3/91.9%, luna 91.0/92.6%, gemini 88.9/84.1% (reverse check fails 73→116);
tuning stopped (sonnet gained <1 point). Draft 3 on the terminal models: `terminal-claude-opus-5` 89.1/90.3%,
`terminal-claude-haiku-4-5` 69.1/61.5% (reverse check fails 129), both ~29 calls/min at 8 workers. Reports per draft `output/kanjify_eval_report_<model>_draftN.txt`.

match_words_to_notes will take `elements_to_match()` (state 3) to its main prompt and
`elements_to_rate()` (state 4) to the secondary one that only sets `match_quality`.
`match_targets.gather_targets()` gathers them: each occurrence is a target holding its element,
so a result is written into that element's `match_data` at whatever depth it is nested, with the
array's part of speech label mapped to the one the word notes use. `states_to_match()` says which
states a run takes: `["match"]`, plus the linked states 4 and 5 with
`replace_existing_matched_words`; the single-word rematch entries take their mode's states
(unprocessed 3, processed 4 and 5, both), and find their notes with `word_array_query_regex`.
Before gathering, `resolve_placeholder_ids()` swaps a new note's negative placeholder id an
earlier run left behind for the id of the note holding it in `new_note_id_field` (the field is
left set, other notes may still hold the placeholder); one no note holds goes back to `["match"]`.
The main prompt asks for a `match_quality` (1-5) with every match or new meaning, and a target is
saved `[note_id, match_quality]`, or `[note_id]` when the response gave no valid one
(`parse_match_quality()`). The prompt shows the target's sentence in plain text with the very
occurrence in `<b>` (`highlighted_sentence()`, without the field's `<i>` context), and each
existing meaning's example sentence with that note's word in `<b>` when the note holds an array
(`example_sentence()`: the occurrence linked to the note, else the first of its word); an old word
list note's sentence is shown as it is. An old word list's words get the same prompt, their
match_quality dropped. Every run also rates the `[note_id]` words (`states_to_rate()`: unless the
run matches them again) with a secondary prompt given only the linked note's meaning and the
`<b>` sentence (`rating_prompt()`, `RATING_INSTRUCTIONS`, same 1-5 scale as the main prompt);
`save_ratings()` makes them `[note_id, match_quality]`. A note with no meaning yet, or not found,
stays `[note_id]`.

clean_meaning's sentences (`clean_meaning.get_sentences_for_note`, the word note's own and those
of the notes linking to it) go through `example_sentence()` too: a note holding an array gives
its sentence with the occurrence linked to the word note in `<b>` (a new note, the first of its
word), so the meaning is cleaned for the occurrence meant when a sentence uses the word twice.

## Deduplicating the vocab notes

One learnable word could own several notes, the old `match_words_to_notes` having made one per
spelling it met. **This was done on the collection on 2026-09-16**; the scripts stay re-runnable,
and the counts below are what that run saw.

`research/vocab_dupes.py --fetch` dumps every vocab note over AnkiConnect and
groups them by canonical key: `canonical_form()` over the sort field minus its `(mN)`/`(kun)`/`(rN)`
markers, with the kanjify policy's helper verbs and copula back in kana (`unkanjified`), read with
the note's reading. 25452 notes made 21656 keys; 219 keys were spelled several ways (559 notes, 256
off-key, 60 studied, 439 word-list links), 2271 keys held several `(mN)` meanings, and 102 sort
field values sat on two notes.

`research/vocab_rekey.py` re-keys and tags those groups; it never deletes. Every note of a group
takes one prefix — the canonical key plus the markers its canonically spelled notes carry — and an
`(mN)`, numbered so the note meant to survive holds the lowest (studied first, oldest first within
that), keeping its own number where that is free and outside numbers untouched; a respelled note
drops its reading markers, and the holes those leave in a spelling's `(rN)` series are listed for
`update_note_reading_markers`. Everything touched is tagged `word-array-duplicate`, a variant also
`word-array-keeper::<nid>`. `--apply` writes only notes still spelled as the dump says, recording
the old values in `output/vocab_dedupe_undo.jsonl` for `--revert`. "Clean dictionary meaning" and
"Deduplicate existing meaning notes" on the tag then collapsed the meanings and repointed the links.
A key that spells a word wrong (まだ → 未だ) goes in `generator.CANONICAL_EXCEPTIONS`.

## Migrating the old word lists

**The migration was run on the collection on 2026-09-16** (name lexicon, migrate, find proper
nouns, judge), so no note should hold an old word list any more; what follows is what it did.

`research/migrate.py`'s `migrate(word_lists, arr)` fits a stored extract_words word list into a
generated array, writing `match_data` in place. It sits under `research/` because the op that
called it is gone; `migrate_fit.py`, `judge_eval.py` and `proper_nouns.py` still use it. The
array is taken as correct, so the only thing carried over is the one thing the generator cannot
produce: the note id of a word already matched. The meaning index goes (a word's position in the
array is what tells two occurrences apart now) and so does the sort field value, which the note
itself has; an entry with no note id therefore carries nothing and is counted, not reported.

An entry finds its element in steps, each needing exactly one element to fit, and the step also
ranks the claim, so that of two entries wanting one word the better-founded one keeps it
instead of both standing down:

| Step | Fits when | Carries over |
| --- | --- | --- |
| `form` | the form and reading as they stand | 3071 |
| `okurigana` | same reading, same kanji (向う -> 向こう) | 19 |
| `written` | same form, the old reading colloquial or wrong (何[なん], ノイローゼ[はいろーぜ]) | 27 |
| `reading` | same reading, part of speech fits the category (する -> 為る, について -> に就いて) | 121 |
| `raw` | the element's raw text is the entry's word (です -> だ, 突き -> 突く) | 62 |

What fits nothing, fits several elements, or is contested by a second link is reported rather
than guessed at: match_words_to_notes can match the word again from the sentence with `<b>`
marking which occurrence it is, which beats a coin toss here. The exception is **several
occurrences of one word**, where the link goes on all of them: the old list naming 為る once for
a sentence with two of them gave it one note and never said which occurrence it meant. This was
first done for particles and the copula only; over the whole collection that left some 450
content-word links lost, so the rare two occurrences with two meanings are left to the matching
step instead.
`lost_note_ids` is the part worth a caller's attention: a lost link is what the op tags a note
for.

Measured by `research/migrate_fit.py` over the 292 sentences of the extract_words fine-tuning
set, which is a hand-checked corpus of the old format: **87.6% of 3768 entries carried over**
(220 of them spread over several occurrences), the rest being 9.0% with no element at all, 1.7%
ambiguous and 0.8% contested. The no-element share is mostly by design - the old lists gave
notes to compound function words the structural rules refuse (には, でも, として), and to て and
ない, which now belong to the verb's inflection chain.

The sentence the array is generated from has its `<i>` context sentences stripped first
(`html_stripping.strip_context_sentences`), the way extract_words strips them: the neighbouring
sentences of the source passage are not what the note is about, so their words should not be
offered to match_words_to_notes. An array therefore reconstructs the field without its context,
not the whole field.

## Results

Against the 23 hand-converted extract_words examples (`research/gold_examples.md`), whose
structure was revised to follow the rules above:

| Measure | Result |
| --- | --- |
| top-level words exactly as in the gold | 100% (280/280) |
| sub-words exactly as in the gold | 99.1% (precision 98.3%): 肥ゆる, a classical form |
| dict_form / reading on matched words | 98.9% / 98.6% |
| match_data (the default flags) | 100% |
| sub-word raw_text equal to the gold's | 116/116 |
| speed | ~3 ms per sentence, after ~1.5 s loading Sudachi and the JMdict index |

Top-level agreement says the gold and the rules agree, not that the rules are right: the gold
was revised to them. The rules came from these same examples, so accuracy on the collection at
large is not measured yet.

On those 23 plus the 22 kanjify_sentence examples: every array reconstructs its sentence, and
of 756 word positions 34 give unbalanced html when wrapped in `<b>`, all of them fixed by
`use_tag_cleaning.apply_tag_fixes`, which now closes and reopens whatever tags a `<b>` span
crosses instead of encloses (`<b><k>A</k>を<k>B</b>C</k>` -> `<b><k>A</k>を<k>B</k></b><k>C</k>`).

## Tokenizers considered

From [awesome-japanese-nlp-resources](https://github.com/taishi-i/awesome-japanese-nlp-resources):

| Tool | Result |
| --- | --- |
| **Sudachi** (sudachi.rs / SudachiPy) | Used. Split modes give word vs sub-word; `normalized_form` gives kanjified lemmas; `core` and `full` dictionaries score the same. |
| himotoki (Python port of ichiran, JMdict-based) | Tested on the same gold: 68.2% exact, 77.5% reachable, 19.6% of sub-words, ~6 s per sentence. Cuts unknown names into single kanji (里\|樹, 藤\|堂) and greedily merges する-verbs and particles. |
| UniDic long-unit-word BERT (KoichiYasuoka) | Tested: fast (2 s for 23 sentences on CPU), handles classical forms (馬肥ゆる), but merges what the gold splits (遂行能力, 侍女たち, くじ入りカプセル) and mangles kana-heavy spans. Adds nothing over the noun-run proposals. |
| MeCab / UniDic, Janome, Vibrato, Vaporetto, Lindera, kagome | Not tested: same short-unit granularity as the MeCab setup word_highlight already uses; they differ in speed. |
| GiNZA | Not tested: Sudachi plus a spaCy dependency parse, no gain for word units. |
| Juman++ / KNP | Not tested: recognizes compound function words, but no practical Windows build. |
| dango | Not tested: abandoned (2021) learner-oriented grouping on Sudachi, the same idea as stage 3. |
| yomikata, mozcpy | For the later furigana-generator phase. |

## Open decisions

- **Nesting depth.** Matches nest literally, so 無しには is 無しに + は with 無しに = 無し + に.
- **Noun forms as verbs** only as sub-words (違い -> 違う in 違い無い). The gold still lists
  top-level 囁き/掬い/入り as verbs (ex. 23), so dict_form scores 274/280 against it.
- **Jukujikun decomposition.** Sharing a jukujikun reading out between sub-words is only done
  for splits the tokenizer already makes. The same derivation could let `decompose` split
  田舎者[いなかもの] into 田舎[いなか] + 者[もの], and it is its own safety check - the pieces of
  a real jukujikun word never add up to its reading (今日 is not 今[いま] + 日[ひ]).

Known limitations: classical forms (肥ゆる tokenizes as 肥 + ゆる); short kana after `<k>`
reversion can confuse Sudachi (`<k> 四[し]の 五[ご]の` as しのごの); colloquial contractions
keep their written form (物ん); a word in kanji JMdict spells with other kanji keeps the note's
kanji (聴く, 在る).

## Running

From the add-on root, with SudachiPy installed in the development Python:

```bash
python word_array/research/setup_resources.py   # the first-use downloads, into user_files/
python word_array/research/evaluate.py          # accuracy against the gold, -q for the summary
python word_array/research/validate.py          # reconstruction, sub-words, <b> wrapping
python word_array/research/write_generated.py   # regenerate research/generated_examples.md
python word_array/research/migrate_fit.py       # the corpus loader; its own report is spent
python word_array/research/sub_readings.py      # parents whose sub-words' readings don't add up
python word_array/research/okurigana_decomp.py  # what the okurigana rules split, and don't
python word_array/research/name_lexicon.py      # what the name lexicon holds and misses
python word_array/research/judge_eval.py build  # the judge's eval set, from the checked export
python word_array/research/judge_eval.py run    # ask the judge (real requests) and score it
python word_array/research/proper_noun_eval.py  # score models on the proper noun op
py -3.10 word_array/research/hand_judge.py      # the hand-judging GUI (localhost)
py -3.10 word_array/research/kanjify_eval.py --model M  # kanjify prompt vs the checked labels
py -3.10 word_array/research/kanjify_survey.py       # collection-wide kanjification survey
py -3.10 word_array/research/kanjify_rekanjify.py    # re-kanjify the survey's sentences
py -3.10 word_array/research/kanjify_fix.py          # apply the fix list, --apply / --revert
py -3.10 word_array/research/vocab_dupes.py --fetch  # vocab notes that are the same word
py -3.10 word_array/research/vocab_rekey.py          # re-key them to one sort field, --apply
py -3.10 word_array/research/unbalanced_tags.py      # array words whose html doesn't balance
py -3.10 word_array/research/canonical_forms.py      # what one spelling per entry changes
pytest test/test_word_array.py                  # skipped until the downloads are there
```

`migrate_fit.py --corpus export` (the default) dry-runs the migration over the whole collection's
exported word lists, invalid data and crashes included; `checked` and `fine_tuning` are the
smaller corpora. The whole export takes some minutes.

An installed `sudachidict_*` package also counts as the dictionary; `SUDACHI_DICT` (a
dictionary name or an absolute path to a `.dic`) overrides both.

`research/generated_examples.md` is committed so that changes in the generator's output show
up as diffs.
