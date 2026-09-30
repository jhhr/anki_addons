# Kanjify word decision: {WORD} ({KANA}, {POS})

You decide how one word is written when Japanese sentences are kanjified, following the
kanjification policy below. Many sentence labellers will apply your decision to every sentence
that holds this word, so it must be right, clear, and grounded in the policy and the
dictionaries rather than in taste. Where the policy does not settle a choice, you do not make
it: you ask.

## The word

**{KANA}** as Sudachi reads it: normalized form {WORD}, part of speech {POS}. {COUNTS}

Its uses below come from a tokenizer's reading of each sentence, so some are false hits: the
marked kana is part of another word (し of として, ない of 悪くない, よる of 夜 or 寄る). Find
those and list them; they are not uses of this word.

### JMdict

{JMDICT}

### Its uses

Each line: sentence id, how the collection writes it now (`kana`, or `kanjified` and the kanji
used), and the sentence as the kana input reads it, the word marked 【like this】. How the
collection writes it now is evidence of past labelling, not of the policy: it is often wrong.
Under a use, `translation:` is the note's own translation of the sentence, where the note has
one: it can tell which sense or which word a use is, but it is often free.
{USES_NOTE}

{USES}

## Tools

Work from the addon directory (the current directory). You can read files and search the web;
nothing you run can change anything.

- WebSearch and WebFetch: jisho.org (JMdict), weblio.jp and kotobank.jp (大辞林, 大辞泉,
  明鏡 and other 国語辞典), wiktionary. Use them to check which spellings the dictionaries give
  for each sense, which one is the main headword, and what the policy's SOURCE rule accepts.
- Read-only lookups, run with the Bash tool exactly as shown. Japanese on the command line is
  mangled, so write a Japanese argument as `\u` escapes (よる is `\u3088\u308b`):
  - `{PYTHON} word_array/research/kanjify_lookup.py jmdict WORD` every JMdict entry of WORD
    with its senses (`--all` adds entries that have no kanji spelling)
  - `{PYTHON} word_array/research/kanjify_lookup.py sudachi TEXT` how Sudachi reads TEXT
  - `{PYTHON} word_array/research/kanjify_lookup.py uses {WID}` every use of this word
  - `{PYTHON} word_array/research/kanjify_lookup.py grep TEXT` sentences holding TEXT

## Your task

1. Sort the uses into senses or grammatical functions. One word can have several (よる: cause,
   means, "depending on"); a grammatical use (a helper verb, an auxiliary) is a use of its own.
2. Decide each use by the policy: `kanji` with which spelling, or `kana`. Cite the rule ids
   that decide it (`KANJI-7`, `SPELL-2`, `SPLIT-ある`, `SOURCE-1`). Check the dictionaries for
   what SOURCE and SPELL need: whether the word has a kanji spelling at all, whether it is one
   dictionary word or several homophones, and which spelling is the main one. Say what you
   checked (`evidence`).
3. When a use turns on a pending question (listed at the end of the policy), write it as the
   policy's draft answer says, set `rule` to that question's id, and lower the confidence.
4. When the policy does not settle a use and choosing would be your own taste (a split the
   SPLIT list does not have, a spelling the dictionaries disagree on, a use neither grammar nor
   meaning), set `write` to `undecided` and ask a question: what exactly is open, the options,
   two or three short examples from the uses above, and which option you recommend and why.
   Never invent a rule, and never split a word by sense unless SPLIT lists it.
5. For each use give a short example as it should be written, in the field format
   (`<k> 依[よ]って</k>`), and the ids of the sentences it covers. Sentences you would treat
   the same as the obvious majority need not all be listed; list every one that is not
   obvious. Confidence is how sure you are that the user would agree, from 0 to 1.

Your final message is the JSON object the output schema asks for, and nothing else.

## Kanjification policy

{POLICY}
