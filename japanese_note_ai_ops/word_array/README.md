# word_array

Generates the word arrays of the extracted-vocab redesign: a partition of a furigana sentence
into dictionary words, each `[raw_text, part_of_speech, dict_form, reading, match_data,
sub_words]`, with tags and punctuation as single-element arrays. Concatenating the top-level
`raw_text` values gives back the sentence (minus `<b>` tags), so any word can be wrapped in
`<b>` without inflection matching.

Status: phase 1 prototype, not wired into any op yet.

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
   て/で/ば, auxiliary いる). しまう, やる, おく and other auxiliaries stay separate words.
4. **Furigana groups are never split between top-level words**, which repairs tokenizer cuts
   like 八紘|一宇 and 業|者.
5. **Multi-word candidates**: n-grams of words that are JMdict entries, looked up as written,
   as tokenized (the `<k>` kana) and with the last word deinflected (様に成った -> 様に成る).
6. **Structure** (below).
7. **Dictionary form and reading.** Readings come from the note's own furigana wherever it has
   it; inflected words take the JMdict reading of their lemma that agrees with the furigana stem.
   A multi-word unit matched with its last word deinflected takes its dictionary form and
   reading from its parts, in the note's spelling (様に成る / ようになる, not ようになる).

A sub-word that ends inside a furigana group gets its own share of the reading, split per kanji
by `kana_highlight`: `見下[みお]ろせた` -> ` 見[み]` + `下[お]ろせた`. A jukujikun group has no
per-kanji split, but it can still be split between two words whose own readings add up to it:
`為替相場[かわせそうば]` -> ` 為替[かわせ]` + `相場[そうば]`, each reading taken from the
sub-word's Sudachi and JMdict readings, the second allowed to be voiced by rendaku
(`土産物[みやげもの]`). A sub-word never comes out as bare kanji: when the readings don't add
up, the word simply keeps no sub-words, which is what stops a jukujikun word being split inside
(今日, 田舎者). A sub-word's reading drops the rendaku of the compound it came from when JMdict
has the plain reading (閏日 -> 日[び] -> ひ).

## Structure

Which words exist follows concrete rules; whether a word is worth matching to a note is the
`"dont_match"` flag's call, not the generator's. So the rules err towards more words: nesting
loses nothing, and the flag is what filters.

**Multi-word units.** Every JMdict match becomes a parent with its words as sub-words
(連れて行く, 様に成る, 先ずは, 何時まで, 足が竦む, 此れは), except where it is not a word of the
text at all:

- function words only (には, のだ, か+の read as 彼の);
- found only through its kana, starting on a particle, and not spelled that way in JMdict:
  は+幾つ read as はいくつ (背屈) or を+持って as をもって (を以って). The text's kana must
  match one of the entry's spellings, with the same kanji or kanjified kana between them, so
  だけの事は有って still matches だけの事はある.

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
  言|語).

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

## The "dont_match" flag

`match_data[0]` is the matched note's id once match_words_to_notes has run; the flag is the
string `"dont_match"` in the same slot, so one check skips both (`match_flags.py`).

Numbers other than 一-九, 十, 二十 and the multipliers start out flagged, and so does a word
built on one (二十八日, 十一時): numbers have been a steady source of junk notes. Everything
else is an AI job's call - `flag_prompt()` numbers every word element and states the rules the
old extract_words prompt used (a compound meaning no more than its parts, a word plus the
particle it takes, the pieces of a yojijukugo), and `apply_flag_response()` applies its answer.
The job is meant for migrated arrays as well as new ones, so it can flag a word that was
matched; `apply_flag_response` returns the note ids that flag unlinked.

`elements_to_match()` is what match_words_to_notes will use once it reads word arrays: the
words that are neither matched nor flagged. Running the job on notes needs the field that will
hold the arrays, which comes with the phase 2 migration, so nothing calls the job yet.

## Migrating the old word lists

`migrate.migrate(word_lists, arr)` fits a stored extract_words word list into a generated
array, writing `match_data` in place. The array is taken as correct, so the only thing carried
over is the one thing the generator cannot produce: the note id of a word already matched. The
meaning index goes (a word's position in the array is what tells two occurrences apart now) and
so does the sort field value, which the note itself has; an entry with no note id therefore
carries nothing and is counted, not reported.

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
marking which occurrence it is, which beats a coin toss here. The exception is a **particle or
the copula**, where a link fitting several occurrences of one word goes on all of them: the old
list naming の once for a sentence with two of them never said which it meant, and two
occurrences of a function word are all but never two notes. A content word is left alone, since
there two occurrences may be two meanings - what the old meaning index was for.
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

- **Surface vs deinflected match.** A JMdict entry for the text as written wins over one for
  its dictionary form: 秋と言った物 gives と言った (conj, "such as") where the gold has と言う,
  but そう言えば stays そう言えば rather than そう言う.
- **Nesting depth.** Matches nest literally, so 無しには is 無しに + は with 無しに = 無し + に.
- **Noun forms as verbs** (囁き -> 囁く, 違い -> 違う as a sub-word of 違い無い) are right for
  the gold but wrong for lexicalized nouns (積り).
- **Jukujikun decomposition.** Sharing a jukujikun reading out between sub-words is only done
  for splits the tokenizer already makes. The same derivation could let `decompose` split
  田舎者[いなかもの] into 田舎[いなか] + 者[もの], and it is its own safety check - the pieces of
  a real jukujikun word never add up to its reading (今日 is not 今[いま] + 日[ひ]).

Known limitations: classical forms (肥ゆる tokenizes as 肥 + ゆる); short kana after `<k>`
reversion can confuse Sudachi (`<k> 四[し]の 五[ご]の` as しのごの); colloquial contractions
keep their written form (物ん); the note's spelling is kept where JMdict's differs (向う, not
向こう).

## Running

From the add-on root, with SudachiPy installed in the development Python:

```bash
python word_array/research/setup_resources.py   # the first-use downloads, into user_files/
python word_array/research/evaluate.py          # accuracy against the gold, -q for the summary
python word_array/research/validate.py          # reconstruction, sub-words, <b> wrapping
python word_array/research/write_generated.py   # regenerate research/generated_examples.md
python word_array/research/migrate_fit.py       # what the migration carries over, and loses
pytest test/test_word_array.py                  # skipped until the downloads are there
```

An installed `sudachidict_*` package also counts as the dictionary; `SUDACHI_DICT` (a
dictionary name or an absolute path to a `.dic`) overrides both.

`research/generated_examples.md` is committed so that changes in the generator's output show
up as diffs.
