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
2. **Sudachi** (`SplitMode.C`), keeping each compound's `SplitMode.A` split for sub-words.
   `normalized_form` turns kana lemmas back into kanjified ones (する -> 為る, これ -> 此れ,
   くださる -> 下さる).
3. **Grouping** morphemes into words: a verb or adjective plus its inflection chain (助動詞,
   て/で/ば, auxiliary いる). しまう, やる, おく and other auxiliaries stay separate words.
4. **Furigana groups are never split between top-level words**, which repairs tokenizer cuts
   like 八紘|一宇 and 業|者.
5. **Multi-word candidates**: n-grams of words that are JMdict entries (the last word also
   tried deinflected: と言った -> と言う), and runs of adjacent nouns missing from JMdict
   (声高々, 配役ミス) as proposals only.
6. **`keep_candidate`** decides which candidates become words. It is a heuristic stand-in
   following the old extract_words rules; see open decisions.
7. **Dictionary form and reading.** Readings come from the note's own furigana wherever it has
   it; inflected words take the JMdict reading of their lemma that agrees with the furigana stem.

A sub-word that ends inside a furigana group gets its own share of the reading, split per kanji
by `kana_highlight`: `見下[みお]ろせた` -> ` 見[み]` + `下[お]ろせた`. Jukujikun can't be split
that way, so there the bracket stays whole on the last piece.

## Results

Against the 23 hand-converted extract_words examples (`research/gold_examples.md`):

| Measure | Result |
| --- | --- |
| top-level words exactly as in the gold | 93.1% (precision 95.6%) |
| gold words among the generated candidates (a perfect keep/drop step) | 98.3% |
| gold sub-words among the candidates | 97.9% |
| dict_form / reading on matched words | 98.9% / 98.6% |
| sub-word raw_text equal to the gold's | 34/35 (the other is a gold slip) |
| speed | ~3 ms per sentence, after ~1.5 s loading Sudachi and the JMdict index |

On those 23 plus the 22 kanjify_sentence examples: every array reconstructs its sentence, and
of 690 word positions 21 give unbalanced html when wrapped in `<b>`, all but one fixed by
`use_tag_cleaning.apply_tag_fixes`. The one left is an expression ending inside a `<k>` span
(`<k> 優劣[ゆうれつ]</k>を<k> 付[つ]け 難[がた]い</k>`), which needs the highlighter to
close and reopen the `<k>` around `</b>`.

The rules were tuned on these same examples, so accuracy on the collection at large is not
measured yet.

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

- **Structure vs review.** The design is moving to words that exist by concrete linguistic
  rules, with a separate AI-set flag for whether each is reviewed. `keep_candidate` still
  makes semantic calls the old way and should become a structural rule once that is settled.
  Since nesting loses nothing, one candidate rule is that every JMdict multi-word match becomes
  a parent (様に成る, 先ずは, 何時まで, 足が竦む, 私達), with the review flag doing the
  selecting. That changes the gold, which keeps those apart today.
- **Which sub-words exist.** The gold splits 耳元, 世界中 and 頭ごなし but not 飛行機, 生徒会,
  照れ屋 or 軽音部. That is study value, not structure. `decomposition_subs` proposes 2-way
  JMdict splits but over-generates on on'yomi compounds (最|近), so it isn't emitted.
- **Noun forms as verbs** (囁き -> 囁く) are right for the gold but wrong for lexicalized nouns
  (積り).

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
pytest test/test_word_array.py                  # skipped until the downloads are there
```

An installed `sudachidict_*` package also counts as the dictionary; `SUDACHI_DICT` (a
dictionary name or an absolute path to a `.dic`) overrides both.

`research/generated_examples.md` is committed so that changes in the generator's output show
up as diffs.
