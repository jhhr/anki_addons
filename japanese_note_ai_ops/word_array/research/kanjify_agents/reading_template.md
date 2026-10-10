# Fix the furigana of {COUNT} sentences

These sentences come from language-learning flash cards, in the field format: each kanji word
is written ` 漢字[かな]`, a space, the kanji, and its reading in brackets. A program or a
labeller found something wrong or doubtful in each one's furigana, listed under it. You write
each sentence again with its furigana right. The user reads your fixes before any goes into a
note, so say plainly what you changed and why.

## How to work

For each sentence:

1. Read it whole and work out what it means; the translation under it helps.
2. Give every kanji a reading. A kanji written with no brackets gets them, numerals included:
   `一 週間[しゅうかん]` -> ` 一[いっ] 週間[しゅうかん]`. Keep the input's groups: the bare kanji
   gets a group of its own, and a neighbouring group's reading changes only where the two
   together are read otherwise (`六 階[かい]` -> ` 六[ろっ] 階[かい]`, `三 階[かい]` ->
   ` 三[さん] 階[がい]`). A reading is what this sentence says: 一日 is いちにち "one day" or
   ついたち "the first", as the sentence means.
3. Correct a reading that is wrong for its kanji or for this sentence (今日 read こんにち where it
   means today, 人 read じん where it is ひと). A reading that is only less common is not
   wrong: leave it.
4. A reading that is several readings (`[よそ, たしょ]`) becomes the one this sentence means.
5. Decide each doubt listed under the sentence: a labeller's fix with its confidence is a
   proposal, not a fact. Take it, write the group another way, or leave it as the input has it,
   and say which.
6. Change nothing else. Outside the brackets the sentence stays exactly as the input has it,
   character for character, tags and spaces included; only a new group gets the space before
   it. A program checks that the sentence without its readings and spaces is the input's.
   The one exception is a clear typo in the text itself (a doubled or dropped kana, a kanji
   whose reading shows the word meant, a stray copy of a word), and only where the sentence and
   its translation leave no doubt what was meant: fix it, set `text_changed`, and say so in
   `changes`. A typo you are not sure of is left as it is and named under `unsure`.
7. Write each group as the collection does: okurigana outside the brackets (` 書[か]く`, not
   ` 書く[かく]`), no kana inside a group's kanji (`ネコ 科[か]`, not `ネコ科[ねこか]`), the
   reading in hiragana unless the input's own reading there is katakana.

When a sentence needs nothing (a doubt that turns out wrong), return it unchanged with no
changes.

## Tools

Work from the addon directory (the current directory); nothing you run can change anything.
Use them when a reading needs checking.

- `{PYTHON} word_array/research/kanjify_lookup.py jmdict WORD` JMdict's entries of WORD, with
  their readings and senses. Write Japanese as code points (よる is `U+3088U+308B`): Japanese
  on the command line is mangled. Run it with the Bash tool exactly as shown.
- `{PYTHON} word_array/research/kanjify_lookup.py sudachi TEXT` how Sudachi splits and reads
  TEXT.

## Sentences

Each: sentence id, the sentence, then what was found. `translation:` is the note's own
translation, where it has one; it is often free.

{SENTENCES}

## Output

Your final message is the JSON object the output schema asks for, and nothing else: for every
sentence, in the order given, its `sid`, the whole sentence with its furigana fixed (`fixed`),
each change (`was`: the input's text, `now`: yours, `why`), `text_changed`, a `confidence` from
0 to 1 that the fixed sentence is right, and under `unsure` anything you left open (empty when
nothing).
