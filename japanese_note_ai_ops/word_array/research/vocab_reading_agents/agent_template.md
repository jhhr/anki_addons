# Reading question batch {BATCH}: {COUNT} words

One Anki vocabulary note spells a word one way and reads it one way. Word arrays built from real
sentences spell it the same way but read it differently. For each of the {COUNT} cases below,
work out which reading is right — or whether the spelling is really two different words — and
**write a plan**. You are not editing anything: the plans are read by a human who decides.

Notes in this batch: {NIDS}

## What you are deciding

Four outcomes, and what each one costs downstream:

- **`array`** — the note's reading is a mistake. `vocab_reading_fix` repairs `vocab-kana` (and
  redraws `vocab-furigana`) to the array's reading. This is the cheapest fix and the only one
  already wired up.
- **`note`** — the note's reading is right and the array elements are wrong. Nothing on the note
  changes; the elements must be unlinked or re-read on the array side, which is a separate pass.
- **`split`** — one spelling, two genuinely different words with different readings and different
  meanings (醜女 しこめ vs しゅうじょ). Neither reading is wrong; the element wants a note of its
  own. Say whether a note for the other reading already exists.
- **`unsure`** — the evidence does not settle it. A real answer, not a failure; say what would
  settle it.

## How to judge

Judge by the reading the word really has, not by which source looks more official.

- **The sentence furigana is generated, not authored.** It comes from an automatic tokenizer and
  is wrong often enough to be treated as evidence, never as proof. A previous model read 素っ惚ける
  as すっほうける purely because one sentence's furigana said so, while its own reasoning named the
  meaning of すっとぼける. When the furigana disagrees with what you know of the word, suspect the
  segmentation: look at whether the tokenizer split a longer form (すっとぼけんな) in the wrong place.
- **Rendaku goes both ways.** A compound may genuinely voice (三つ子 みつご) or genuinely not. Neither
  the note nor the array is the reliable side.
- **Phonology is evidence.** Gemination happens before k, t, s and p, not before ふ.
- **Check every sentence, not the three quoted.** `links NID` shows them all with the element as
  the array actually stores it.
- **Another note holding the other reading is the strongest sign of a `split`** — but check that
  the other note is a real, studied note and not a duplicate someone made by accident.

## Tools

Work from `C:\Users\jrk\AppData\Roaming\Anki2\addons21\anki_addons\japanese_note_ai_ops`.
Everything here is **read-only** — none of it can change the collection.

**Never put Japanese text on a command line; it gets mangled.** Japanese only ever goes into a
file written with the Write tool, or comes back out of a file you Read.

    py -3.10 word_array/research/vocab_lookup.py note NID [NID...]
    py -3.10 word_array/research/vocab_lookup.py same NID   --out {WORK}\same_NID.txt
    py -3.10 word_array/research/vocab_lookup.py links NID  --max 30 --out {WORK}\links_NID.txt
    py -3.10 word_array/research/vocab_lookup.py find QUERYFILE --out {WORK}\find_NID.txt

- `note` prints a note's fields. `same` lists every other note answering to the same spelling, and
  every note with the same reading — this is what a `split` turns on. `links` lists the sentences
  whose array links this note, each with the element's `raw_text / pos / dict_form / reading`.
- `find` runs a **live** Anki search; put the query in a UTF-8 file with the Write tool first
  (e.g. `"vocab-kana:*きしょく*"`, `note:"Japanese vocab note" vocab:*頭*`).
- Always pass `--out` and then **Read** the file. Piping Japanese through the console mangles it.
- You may also use your own knowledge of Japanese, and WebSearch if a word is genuinely obscure.

## Your plan, one file per case

Write `{PLANS}\nid_NID.md` with the Write tool — one file per case, this exact skeleton, then
whatever else is worth saying:

```markdown
# NID — SPELLING (note READING / array READING)

**Verdict:** array | note | split | unsure
**Confidence:** high | medium | low
**Proposed action:** one sentence naming exactly what should change, and where.

## What the evidence says
Freeform. What you looked up, what it showed, and what settled it. Name the sentences or notes
you are relying on. If you overturned what the two models said, say why they went wrong.

## Risks
What would make this the wrong call, and anything the human should look at before agreeing.
```

Be concrete in **Proposed action**: `repair vocab-kana on nid 123 from けしき to きしょく`,
`leave the note; unlink the 4 elements reading もり`, `split: make a new note for 醜女 しゅうじょ,
the existing note keeps しこめ`. If the right action is something the four verdicts do not cover,
say so plainly — that is useful, and the plan is read by a human, not executed by a script.

## The cases

{CASES}

## Report

When every case has a plan file, reply with one line per case — `nid NID: VERDICT (confidence) —
proposed action` — then a totals line, and a line naming any case where you disagree with both
models. Nothing else; the detail belongs in the plan files.
