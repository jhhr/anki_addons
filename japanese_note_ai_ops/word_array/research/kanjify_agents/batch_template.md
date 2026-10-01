# Kanjify sentences: {COUNT} sentences

You kanjify Japanese sentences from language-learning flash cards, following the kanjification
policy and the word decisions below. Your output becomes a reference label that a kanjifying
program is scored against, so consistency with the policy matters more than anything: two
labellers given the same sentence must write the same thing. You do not invent policy, but you
apply it to every word: a missed kanjification is as wrong as a wrong one. Only where a real
choice remains that neither the policy nor a word decision settles do you leave the word as the
input has it and hand it back.

## How to work

For each sentence:

1. Read it whole and work out its meaning; the context decides homophones and senses.
2. Look at every word written in hiragana or katakana, and every し, さ, せ, す, なる / なら /
   なり / なっ, ない, ある, いる, いう, こと, もの, よう, ところ in it. For each decide: kana
   (a KANA rule, or no dictionary spelling by SOURCE), or kanji (a KANJI rule or a word
   decision), and with which spelling (SPELL, SPLIT, the decision).
3. A word listed under "Word decisions" follows its decision: find the use that fits this
   sentence and write it that way. Where a decision and the policy disagree, the policy wins:
   a decision can predate the policy's latest answers. Every other word is settled by the policy itself, and most
   words are: a content word with a dictionary kanji spelling is kanjified with its main
   spelling (SOURCE-1, SPELL-2), whether or not it has a decision. Ordinary words written in
   kana (林檎, 鞄, 一番, 直ぐ, 所謂, 擽る, 梳かす, 成る程) are kanjified like any other; when you
   are unsure a spelling exists or which is the main one, look it up.
4. Hand a word back only when a real choice remains that neither the policy nor a decision
   settles: several kanji fit its meaning here and SPELL, SPLIT and the decisions do not pick
   one, or it is unclear whether its spelling counts under SOURCE, or whether this use carries
   meaning or only does grammar (BASIC). Then leave it as the input has it and list it under
   `pending` with the reason. Never hand a word back only because it has no decision: that
   leaves it in kana, and a word that should be kanjified is then wrong in the reference.
5. Write the sentence in the field format (FMT rules): everything outside your `<k>` spans stays
   exactly as the input has it, turning your spans back into kana must give the input, and
   every span holds a kanji[reading] group. A program checks this and rejects the row if not.
6. Check the input's own furigana too: a reading that is wrong for its kanji or for this
   context (今日 read こんにち where it means today), okurigana doubled or missing (終[おわ]わる),
   a reading split wrongly across a word's kanji. A program already lists a missing space
   before a group, kana written inside a kanji word's group (ネコ科[ねこか]) and a kanji with no
   reading at all; don't report those. Never fix it in
   `kanjified` (that must keep the input's text); report each under `furigana` with the
   corrected text. A sentence with broken furigana is kept out of the reference set until the
   note is fixed, so report only real errors, not a reading that is merely less common.

## Tools

Work from the addon directory (the current directory); nothing you run can change anything.
Use them only when a sentence needs it: most do not.

- `{PYTHON} word_array/research/kanjify_lookup.py jmdict WORD` JMdict's entries of WORD with
  their senses. Write Japanese as code points (よる is `U+3088U+308B`): Japanese on the
  command line is mangled. Run it with the Bash tool exactly as shown.
- `{PYTHON} word_array/research/kanjify_lookup.py sudachi TEXT` how Sudachi reads TEXT: for a
  loanword's kanji spelling, KANJI-16 needs it read as the loanword.

## Word decisions

The decided words that occur in these sentences. Each use says how to write it and which rule
decided it; cite `decision:<word id>/<use number>` for a span it decides.

{DECISIONS}

## Sentences

Each line: sentence id, then the input sentence in the field format (furigana as ` 漢字[かな]`).
Under a sentence, `translation:` is the note's own translation of it, where the note has one.
It can tell which word or sense the sentence means (カキ: an oyster or a persimmon), but it is
often free, and it is never part of the sentence or a reason to kanjify a word or not.

{SENTENCES}

## Output

Your final message is the JSON object the output schema asks for, and nothing else: for every
sentence, in the order given, its `sid`, the whole kanjified sentence (`kanjified`), for each
`<k>` span you wrote its text, the rule id or decision it follows (`basis`) and a confidence
from 0 to 1, its `pending` words, and its `furigana` problems. Put a question about the policy
itself under `questions`.

## Kanjification policy

{POLICY}
