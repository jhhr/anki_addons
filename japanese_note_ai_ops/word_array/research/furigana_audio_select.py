"""Pick the furigana audio eval's lines from a subs2srs export: mostly lines whose readings a
tokenizer is likely to get wrong, plus a random share that shows what an ordinary line costs.

Each line's kanji words (`word_array/kanji_words.py`, the units the op checks and writes) are
listed with Sudachi's reading, the reading the captions give, if any, and why the word is hard:

- `name`: a name the captions read in katakana somewhere in the show (猫猫(マオマオ)) or that the
  labelling gave a reading by ear (`furigana_audio.read_names`), found wherever it is written,
  or a word Sudachi tags as a proper noun in this line. The captions
  read a name at its first mention in an episode only, so most mentions have no reading, and
  Sudachi reads them as common words (猫猫 ねこねこ, 高順 たかのぶ). A one-kanji name is
  matched only where Sudachi also takes it for a proper noun: 馬 is a family name read まー
  and also the horse. The captions read some plants and loanwords in katakana too (石楠花
  シャクナゲ, 可可阿 カカオ), and those come along as names: they are as hard;
- `caption_word`: a word the captions read in hiragana somewhere in the show (妃(きさき),
  薬師(くすし), 主(あるじ)): the caption writer thought it needed one, and Sudachi often reads
  it otherwise;
- `ambiguous`: a word Sudachi itself reads two ways in the show (他 ほか and た, 方 ほう and
  かた), the second way often enough to be no slip (`second_reading`), or one of the classic
  cases in `HETERONYMS`. JMdict cannot pick these: without the
  readings' frequency marks it gives over a thousand of the show's words two readings or more;
- `number`: a number with its counter, one word (10日, 七日, 一匹, 三十分): its reading belongs
  to the whole and seldom divides by kanji (とおか, なのか, いっぴき);
- `split`: a kanji run Sudachi cut into single kanji or around one it could not read (神|美,
  響|迂), one word: a name, mostly, which the captions never read;
- `inline`: the word has its caption reading in this very line, which makes the line a check
  on a transcriber: that word's reading is known. The `inline` stratum takes only the ones
  Sudachi reads otherwise (妃(きさき) as ひ), known tokenizer errors with their answer.

The lines are drawn in four strata: `names`, a name spoken without its reading; `readings`,
another hard word; `inline`, a hard word with its reading in the line, which needs no labelling
to score a transcriber on; and `random`, from the rest. Within the first three, the words take
turns (each name once a round, then the next), so one frequent word cannot fill a stratum, and
lines are shuffled by `--seed`, so a run picks the same lines again. Cards whose
text and audio don't match (`furigana_audio.is_noisy`) and cards with no kanji spoken are never
picked.

Writes `selection.jsonl` (one picked card per row) and `caption_readings.jsonl` (every reading
the captions give, the seed of the show's name list) to `evals/furigana_audio/` of the test
data checkout.

    python word_array/research/furigana_audio_select.py [--count 400] [--seed 1] [--tsv FILE]
    python word_array/research/furigana_audio_select.py --reword

A line the labelling corrected (`furigana_audio.read_fixes`: a caption that is not what the
clip says, or a line the clip does not hold) is read as corrected.

`--reword` keeps the picked cards, whose clips are copied already, and finds their words again
with the rules, names and corrections as they are now. The runs then need scoring again and
the label queue building again; labels given to a word whose span changed no longer apply to
it (`furigana_audio.label_for`).
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence

import furigana_audio as fa
from _bootstrap import load

if TYPE_CHECKING:
    # `load()` gives a module object, so what comes out of it is untyped; the real modules are
    # named for type checking only (the runtime import is what _bootstrap avoids).
    from japanese_note_ai_ops.word_array import kanji_words as kw
    from japanese_note_ai_ops.word_array.kanji_words import Word
else:
    kw = load("kanji_words")
    Word = kw.Word

# The classic cases of a spelling read two ways by context, matched on the surface or the
# dictionary form, so that each is in the pool even where Sudachi read it one way only
HETERONYMS = frozenset(
    "他 方 何 私 今日 明日 昨日 一日 一人 二人 上手 下手 人気 大事 本当 辛い 開く 空く 行う 入る".split()
)
STRATA = ("names", "readings", "inline", "random")
# The share of the count each stratum gets; random takes the rest, and what the others cannot
# fill
SHARES = {"names": 0.35, "readings": 0.35, "inline": 0.1}
# A reading of a surface counts as Sudachi's second only when it gives it this often and for
# this share of the surface's uses: 様 is さま 225 times and よう twice, inside words it cut
SECOND_READING_USES = 3
SECOND_READING_SHARE = 0.05

@dataclass
class Card:
    row: fa.Row
    lines: list[str]  # spoken lines, inline readings kept as the captions write them
    words: list[Word]

    @property
    def reasons(self) -> list[str]:
        """`kind:surface` for every hard word, in line order, each once."""
        seen: dict[str, None] = {}
        for w in self.words:
            for kind in w.kinds:
                seen[f"{kind}:{w.surface}"] = None
        return list(seen)


@dataclass
class ShowReadings:
    """Every reading the captions give over the show, by surface: katakana ones are names."""

    names: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))  # katakana
    words: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))  # hiragana
    episodes: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add(self, row: fa.Row, base: fa.BaseFinder = fa.whole_run) -> None:
        for r in fa.all_inline_readings(row.jp, base):
            (self.names if r.katakana else self.words)[r.surface][r.reading] += 1
            self.episodes[r.surface].add(row.episode)

    def rows(self) -> list[dict]:
        out = []
        for script, table in (("katakana", self.names), ("hiragana", self.words)):
            for surface in sorted(table, key=lambda s: (-sum(table[s].values()), s)):
                out.append(
                    {
                        "surface": surface,
                        "script": script,
                        "readings": dict(table[surface].most_common()),
                        "episodes": sorted(self.episodes[surface]),
                    }
                )
        return out


def jmdict_readings(form: str) -> set[str]:
    """Every reading of the JMdict entries spelled `form`, as the op looks them up."""
    return load("jmdict_index").spelling_readings(form)


def read_cards(
    rows: Sequence[fa.Row],
    tokenize: Callable[[str], Sequence],
    show: ShowReadings,
    base: fa.BaseFinder = fa.whole_run,
    fixes: Optional[dict[tuple[str, int], dict]] = None,
    named: Iterable[str] = (),
) -> list[Card]:
    """Every card that can be picked, its kanji words found and the hard ones marked: a line
    as `fixes` corrected it, if it did, and the names given by ear (`named`) as names."""
    names = set(show.names) | set(named)
    cards: list[Card] = []
    sudachi_readings: dict[str, Counter] = defaultdict(Counter)
    parsed = []
    for row in rows:
        if fa.is_noisy(row.jp):
            continue
        lines = fa.spoken_lines(row.jp)
        for n in range(len(lines)):
            fix = (fixes or {}).get((row.id, n))
            if fix is not None:
                # An empty line keeps its place, so that the lines after it keep their numbers
                lines[n] = fix["text"]
        per_line = []
        for line in lines:
            text, readings = fa.split_readings(line, base)
            morphemes = list(tokenize(text))
            for m in morphemes:
                if fa.KANJI_RE.search(m.surface()):
                    sudachi_readings[m.surface()][fa.to_hiragana(m.reading_form())] += 1
            per_line.append((text, readings, morphemes))
        parsed.append((row, lines, per_line))
    two_ways = {s for s, c in sudachi_readings.items() if second_reading(c)}
    for row, lines, per_line in parsed:
        words: list[Word] = []
        for n, (text, readings, morphemes) in enumerate(per_line):
            line_words = kw.kanji_words(
                n, text, morphemes, readings, names, lambda s: kw.numeral_reading(s, tokenize)
            )
            for w in line_words:
                lemmas = {m.dictionary_form() for m in morphemes if w.start <= m.begin() < w.end}
                if w.surface in show.words and "name" in w.kinds and w.caption is None:
                    # A word the captions read in hiragana is no name, whatever Sudachi's
                    # dictionary says: 薬師 is a temple there, the apothecary くすし here
                    w.kinds.remove("name")
                if "name" not in w.kinds and w.surface in show.words and w.caption is None:
                    w.kinds.append("caption_word")
                if "name" not in w.kinds and (
                    w.surface in two_ways or w.surface in HETERONYMS or lemmas & HETERONYMS
                ):
                    w.kinds.append("ambiguous")
                if w.caption is not None:
                    w.kinds.append("inline")
            words.extend(line_words)
        if words:
            cards.append(Card(row, lines, words))
    return cards


def second_reading(readings: Counter) -> bool:
    """Whether Sudachi's readings of one surface show a second reading of its own."""
    if len(readings) < 2:
        return False
    second = readings.most_common(2)[1][1]
    return second >= SECOND_READING_USES and second >= SECOND_READING_SHARE * sum(
        readings.values()
    )


def round_robin(
    pools: dict[str, list[Card]], want: int, taken: set[str], rng: random.Random
) -> list[Card]:
    """Up to `want` cards, one from each pool in turn, busiest pool first; a card is taken once."""
    order = sorted(pools, key=lambda k: (-len(pools[k]), k))
    queues = {k: rng.sample(pools[k], len(pools[k])) for k in order}
    picked: list[Card] = []
    while len(picked) < want and any(queues.values()):
        for key in order:
            queue = queues[key]
            while queue and queue[-1].row.id in taken:
                queue.pop()
            if not queue:
                continue
            card = queue.pop()
            taken.add(card.row.id)
            picked.append(card)
            if len(picked) == want:
                break
    return picked


def select(cards: Sequence[Card], count: int, seed: int) -> list[tuple[str, Card]]:
    """(stratum, card) for `count` cards, or every card when there are fewer."""
    rng = random.Random(seed)
    names: dict[str, list[Card]] = defaultdict(list)
    others: dict[str, list[Card]] = defaultdict(list)
    inline: dict[str, list[Card]] = defaultdict(list)
    for card in cards:
        for w in card.words:
            if "name" in w.kinds and w.caption is None:
                names[w.surface].append(card)
            for kind in ("caption_word", "ambiguous", "number"):
                if kind in w.kinds:
                    others[f"{kind}:{w.surface}"].append(card)
            if w.caption is not None and w.caption != w.sudachi:
                inline[w.surface].append(card)
    taken: set[str] = set()
    out: list[tuple[str, Card]] = []
    for stratum, pools in (("names", names), ("readings", others), ("inline", inline)):
        want = round(count * SHARES[stratum])
        out.extend((stratum, c) for c in round_robin(dedupe(pools), want, taken, rng))
    rest = [c for c in cards if c.row.id not in taken]
    out.extend(("random", c) for c in rng.sample(rest, min(len(rest), count - len(out))))
    return out


def dedupe(pools: dict[str, list[Card]]) -> dict[str, list[Card]]:
    """Each pool with a card once, though the card has the word twice."""
    return {k: list({c.row.id: c for c in v}.values()) for k, v in pools.items()}


def selection_row(stratum: str, card: Card) -> dict:
    r = card.row
    return {
        "id": r.id,
        "episode": r.episode,
        "audio": r.audio,
        "stratum": stratum,
        "reasons": card.reasons,
        "jp": r.jp,
        "en": r.en,
        "lines": card.lines,
        "words": [w.row() for w in card.words],
    }


def sudachi_tokenizer() -> Callable[[str], Sequence]:
    # Imported here so the tests, which tokenize with a stand-in, run without SudachiPy
    from sudachipy import Dictionary, SplitMode

    resources = load("resources")
    tokenizer = Dictionary(dict=resources.sudachi_dictionary()).create()
    return lambda text: tokenizer.tokenize(text, SplitMode.C)


def summary(picked: Sequence[tuple[str, Card]], cards: Sequence[Card]) -> str:
    by_stratum = Counter(s for s, _ in picked)
    kinds = Counter(k for _, c in picked for w in c.words for k in w.kinds)
    names = {w.surface for _, c in picked for w in c.words if "name" in w.kinds}
    episodes = {c.row.episode for _, c in picked}
    words = sum(len(c.words) for _, c in picked)
    return "\n".join(
        [
            f"{len(picked)} of {len(cards)} pickable cards: "
            + ", ".join(f"{s} {by_stratum[s]}" for s in STRATA),
            f"{words} kanji words, of them "
            + ", ".join(f"{k} {n}" for k, n in kinds.most_common()),
            f"{len(names)} distinct names, {len(episodes)} episodes",
        ]
    )


def reword(picked: Sequence[dict], cards: Sequence[Card]) -> tuple[list[dict], int]:
    """The picked rows with their words found again, in the same order and strata, and how
    many cards' words changed. A card no longer pickable keeps its row."""
    by_id = {c.row.id: c for c in cards}
    rows = []
    changed = 0
    for row in picked:
        card = by_id.get(row["id"])
        new = selection_row(row["stratum"], card) if card else row
        changed += new["words"] != row["words"]
        rows.append(new)
    return rows, changed


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tsv", type=Path, default=fa.EXPORT, help="the subs2srs export")
    ap.add_argument("--count", type=int, default=400, help="cards to pick (default 400)")
    ap.add_argument("--seed", type=int, default=1, help="shuffle seed (default 1)")
    ap.add_argument("--reword", action="store_true", help="the same cards, their words again")
    args = ap.parse_args(argv)

    rows, skipped = fa.read_export(args.tsv)
    for s in skipped:
        print(f"skipped {s}", file=sys.stderr)
    base = fa.captions.ReadingBase((row.jp for row in rows), jmdict_readings)
    show = ShowReadings()
    for row in rows:
        show.add(row, base)
    cards = read_cards(rows, sudachi_tokenizer(), show, base, fa.read_fixes(), fa.read_names())
    if args.reword:
        old = fa.read_jsonl(fa.SELECTION)
        if not old:
            print(f"{fa.SELECTION} is missing or empty: nothing to reword", file=sys.stderr)
            return 2
        new, changed = reword(old, cards)
        fa.write_jsonl(fa.SELECTION, new)
        print(f"{changed} of {len(new)} cards' words changed; wrote {fa.SELECTION}")
        return 0
    picked = select(cards, args.count, args.seed)
    picked.sort(key=lambda sc: sc[1].row.id)
    fa.write_jsonl(fa.SELECTION, (selection_row(s, c) for s, c in picked))
    fa.write_jsonl(fa.CAPTION_READINGS, show.rows())
    print(summary(picked, cards))
    print(f"wrote {fa.SELECTION} and {fa.CAPTION_READINGS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
