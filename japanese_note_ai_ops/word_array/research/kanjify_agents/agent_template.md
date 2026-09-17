# Kanjify label fix: {TITLE}

You are fixing hand-labelled Anki notes so that the word **{WORD}** ({POS}) is written consistently
across all of them, following the kanjification policy below. Work through every note listed under
"Items" one at a time. Reason carefully about each one — a wrong edit costs more than a skipped one.

## Tools

Work from `C:\Users\jrk\AppData\Roaming\Anki2\addons21\anki_addons\japanese_note_ai_ops`. Use the
**PowerShell** tool for commands. **Never put Japanese text on a command line** (it gets mangled);
Japanese only ever goes through files written with the Write tool.

1. Read a note's kanjified sentence field:
   `py -3.10 word_array/research/kanjify_note.py get NID`
   prints `base <hash>` on the first line, then the field's exact current value.
2. Decide the new value (rules below). Write it — the **whole field**, with only your change — to a
   file with the Write tool: `{SCRATCH}\{SLUG}_NID.txt` (UTF-8, one line, no trailing newline needed).
3. Write it back:
   `py -3.10 word_array/research/kanjify_note.py set NID <hash> {SCRATCH}\{SLUG}_NID.txt`
   It refuses (exit 1, reason printed) when the note changed since your `get` (another agent edits
   the same note: `get` again and redo your change on the new value), when the text no longer reads
   the same (you may only add or remove kanjification, never change words, furigana readings or
   punctuation), or when `<k>`/`</k>` don't pair up. Fix and retry; never work around a refusal.

Every write is logged for undo, so a mistake is recoverable, but be careful anyway.

## Field format

The field is a Japanese sentence with furigana as `漢字[かな]`, a space before each furigana group
(` 本[ほん]`). Words the labeller kanjified from kana are wrapped in `<k>` tags:
`<k> 此[こ]れ</k>`, `<k> 為[し]てみます</k>`, `<k> 付[つ]き 合[あ]い</k>`.

- **To kanjify** a kana word: replace it with `<k> 漢字[reading]okurigana</k>` — a space right after
  `<k>`, the kanji, its reading in brackets (the kana that the kanji replaces), then the okurigana
  and any inflection/auxiliary written in kana, then `</k>`. `しかないの` → `しか<k> 無[な]い</k>の`;
  `なりました` → `<k> 成[な]りました</k>`; `していた` → `<k> 為[し]ていた</k>`. Two kanji in one word
  each get their own group: `つきあい` → `<k> 付[つ]き 合[あ]い</k>`. Keep the kana of the original
  exactly (a katakana word keeps katakana in the reading: `<k> 馬鹿[バカ]</k>`; colloquial `もん` stays
  `物[もん]`). If the word is directly adjacent to an existing `<k>…</k>` span, put it inside that
  span with a space between the groups instead of opening a second span.
- **To un-kanjify** a `<k>` word: replace the whole span with the kana it reads:
  `<k> 位[くらい]</k>` → `くらい`, `<k> 無[な]い</k>` → `ない`. If the span holds several groups and only
  one should go back to kana, keep the tags around the rest: `<k> 為[し]て 来[き]た</k>` → `<k> 為[し]てきた</k>`.
- **To respell** with other kanji: change only the kanji, keep the reading: `<k> 成[な]りました</k>` →
  `<k> 生[な]りました</k>`.
- Leave everything else untouched: other words, `<b>`, `<i>`, `<br>`, spaces, punctuation. Words
  already in kanji in the source (without `<k>`) are not yours to change.

## Kanjification policy

The basic test: **kanjify a word when it carries a meaning of its own; leave it in kana when it only
does grammar.** Whether the word conjugates is not the test.

Kana (do not kanjify; un-kanjify if a label did):
- the copula である in all forms (である, であった, であり, であって, であれば, であろう); だ, です.
- ない as the negative auxiliary: 食べない, ではない / じゃない / ではなかった, 高くない / くなかった.
- て-form helper verbs: ている, てある, てみる, てくる, ていく, てくれる, てしまう, ておく, てもらう,
  ていただく, てください, てあげる, てやる, ておる, ていらっしゃる, てまいる, and contractions (てる,
  とく, ちゃう, とる = ておる). The helper's kana goes inside the preceding verb's `<k>` when that verb is
  kanjified: `<k> 為[し]てみます</k>`.
- て-form patterns: てほしい, てもいい / てもよい, てはいけない, てはならない, てもかまわない, てはだめ.
- として (no 為る in it), あげる meaning "to give", そんな/こんな/あんな/どんな, filler なんか, exclamatory
  もう, もっと, particles.
- Auxiliaries (れる/られる/せる/させる/ます/た/たい/らしい/ようだ's だ) are kana. A kanjification of one of
  these is a labelling slip (e.g. れる written as 様).

Kanji (do kanjify):
- 無い as a standalone word (しかない, 必要ない, 見たことない → 無い; なかった, なくて too).
- 居る, 行く, 有る as standalone verbs (家にいる, あっちにいく, 本がある).
- する as 為る everywhere, suru-verbs included (勉強する → 勉強<k> 為[す]る</k>, した → 為[し]た,
  しよう → 為[し]よう, どうしよう → 如何 + 為[し]よう, not 仕様).
- 成る in all uses: ようになる, ことになる, くなる, となる. But 実が生る (fruit grows) is 生る.
- The formal nouns 事/物/為/様/所 (こと/もの/ため/よう/ところ) in all uses, grammatical ones included.
- による/によって/により/によれば/によると: 因る when it gives a cause (事故に因って壊れた), 依る for
  means / "depending on" / "according to" (人に依って違う, 天気予報に依ると).
- Words with several kanji spellings take the kanji that fits the meaning here: 有る (possession,
  occurrence) vs 在る (location, existence somewhere); 付く (attach) vs 就く (take a position, に就いて
  = about) vs 突く vs 吐く; 言う vs 行う; 内 (within) vs 家 (home); 達 vs 等 (plural); 貴方 vs 方; 置く
  vs 於く; 始め vs 初め; 稍 vs 漸; 物 vs 者 (もの/もん: thing vs person).
- Also kanjified: 此の/其の/彼の, 此れ/其れ/彼れ, 何 (なに/なん, どの → 何の, なんて/なんか as 何か when it
  means "something"), 如何 (どう), 然う (そう), 迄, 丈 (だけ), 位 (くらい: the particle くらい/ぐらい IS
  kanjified as 位 — this is a word with meaning "about, extent"), 等 (など, ら), 乍ら, 遣る, 御 (お/ご),
  唯/只, 亦 (また), 嗚呼 (ああ), 否 (いや), 君, 時, 方, 序で, 御蔭/御陰 (use 御蔭), 済む, 彼奴, 出来る,
  振り, 小父/叔父/伯父 by meaning, やすい as 易い.

When the policy says nothing about a use, apply the basic test and keep the majority spelling of
this word across the notes (given under "This word") unless it is clearly wrong there.

## Items

{ITEMS}

Each item is one note. `nid` is the note id; the context shows about 10 characters of the sentence
in kana form around the word, which is marked 【like this】. The field itself may show the same
passage with kanji and furigana — find the passage by reading the field, not by string search.
A note can appear in several items when the word occurs in it more than once.

Contexts are automatic (a tokenizer's reading of the label), so some items are false hits: the
marked kana may be part of another word (し of として, し in 勉強し直す, ない of 悪くない). Such items
are left as they are — say so in your report.

## Report

When every item is done, reply with one line per item: `nid NID: written — what changed`, `nid NID:
left — why`, or `nid NID: refused — reason`, then one line of totals. Nothing else.
