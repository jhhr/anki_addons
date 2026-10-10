"""Pick the furigana audio eval's lines from a subs2srs export: mostly lines whose readings a
tokenizer is likely to get wrong, plus a random share that shows what an ordinary line costs.

Each line's kanji words are listed with Sudachi's reading, the reading the captions give, if
any, and why the word is hard:

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
- `number`: a number with the counter after it, or one with a kanji in it, as one word, its
  digits too (10日, 七日, 一匹, １万): its reading belongs to the whole and seldom divides by
  kanji (とおか, なのか, いっぴき). A number with digits is read as Sudachi reads it spelt in
  kanji numerals (`furigana_audio.kanji_numerals`): it reads 10 digit by digit. A number
  Sudachi cut into a numeral and another word (三|十分, さん|じゅうぶん, enough) is read by
  itself, where Sudachi reads 三十|分;
- `split`: a kanji run Sudachi cut into single kanji (神|美, 羅|半) or into pieces one of which
  it could not read (響|迂), taken as one word: a name, mostly, which the captions never read.
  Kanji by kanji its parts get readings of their own (神 かみ), and an unread one has no sound,
  so the audio of the whole would go to its neighbour. A run cut into longer pieces stays cut
  (物置|小屋, 柘榴|宮): the dictionary knows those pieces;
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
from typing import Callable, Iterable, Optional, Sequence

import furigana_audio as fa
from _bootstrap import load

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

# Sudachi's part of speech for a numeral
NUMERAL = ("名詞", "数詞")
# The kanji a number is spelt with, as `furigana_audio.kanji_number` writes it
NUMERAL_KANJI = frozenset(fa.KANJI_DIGITS + "十百千万億兆")


@dataclass
class Word:
    """A kanji word of a spoken line: a Sudachi morpheme, or several where a name or an inline
    reading spans them. `start` and `end` are offsets in the line without its inline readings."""

    line: int
    start: int
    end: int
    surface: str
    # Sudachi's reading, the morphemes' joined; of a number with digits, of it in kanji
    sudachi: str
    caption: Optional[str] = None  # the captions' reading in this line, hiragana
    kinds: list[str] = field(default_factory=list)

    def row(self) -> dict:
        return {
            "line": self.line,
            "start": self.start,
            "end": self.end,
            "surface": self.surface,
            "sudachi": self.sudachi,
            "caption": self.caption,
            "kinds": self.kinds,
        }


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


class ReadingBase:
    """`furigana_audio.BaseFinder` for one show: which tail of a kanji run an inline reading is
    for, tried in turn:

    1. the longest tail JMdict reads so (高級妓楼(ぎろう) is 妓楼, 元嫁姑(しゅうとめ) 姑);
    2. the shortest the captions read so elsewhere standing alone (鈴麗公主(ひめ) is 公主, read
       so twice on its own);
    3. what follows the longest head the captions read elsewhere on its own, a name mostly
       (白鈴姐(ねえ) is 姐, 白鈴 being read ぱいりん);
    4. the longest with a kana of reading for each kanji at least (好奇心旺盛(おうせい) cannot be
       more than four kanji);
    and the whole run when none fits. 義父上(ちちうえ) comes out 父上, though the 義 is not said
    either: a guess the audio would correct.
    """

    def __init__(self, rows: Iterable[fa.Row], dictionary: Callable[[str], set[str]]):
        self.alone: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            for r in fa.all_inline_readings(row.jp):
                self.alone[r.surface].add(r.reading)
        self.dictionary = dictionary

    def __call__(self, run: str, reading: str) -> int:
        tails = [run[len(run) - n :] for n in range(len(run), 0, -1)]  # longest first
        for tail in tails:
            if reading in self.dictionary(tail):
                return len(tail)
        for tail in reversed(tails):
            if tail != run and reading in self.alone.get(tail, ()):
                return len(tail)
        for n in range(len(run) - 1, 0, -1):
            if run[:n] in self.alone:
                return len(run) - n
        for tail in tails:
            if len(tail) <= len(reading):
                return len(tail)
        return len(run)


def jmdict_readings(form: str) -> set[str]:
    """Every reading of the JMdict entries spelled `form`."""
    jmdict = load("jmdict_index")
    return {reb for kebs, rebs, _ in jmdict.lookup(form) if form in kebs for reb in rebs}


def kanji_words(
    line_no: int,
    text: str,
    morphemes: Sequence,
    readings: Sequence[fa.InlineReading],
    names: Iterable[str],
    read: Optional[Callable[[str], str]] = None,
) -> list[Word]:
    """The line's kanji words: one per kanji morpheme, the morphemes an inline reading or a
    name of `names` spans merged into one word, and so are those of a number (`number_spans`).
    A span the tokenizer cut across takes in the whole of each morpheme it touches, so the word
    may run past it (玉葉|妃 for 玉葉). An inline reading covers its kanji only, 噛(か)みつい, so
    the word's caption takes in the kana around them as written (かみつい); a word with other
    kanji around them gets none. `read` gives the reading of a number with digits, which
    Sudachi reads digit by digit, and of one it cut into a numeral and another word (三|十分,
    じゅうぶん); without it the morphemes' readings are joined. Last, a kanji run Sudachi cut
    where it should not have (`split_runs`) is one word."""
    spans: list[tuple[int, int, Optional[str], bool]] = []  # start, end, caption, is_name
    for r in readings:
        spans.append((r.start, r.end, r.reading, r.katakana))
    taken = [False] * len(text)
    for s, e, _, _ in spans:
        taken[s:e] = [True] * (e - s)
    proper = {(m.begin(), m.end()) for m in morphemes if is_proper_noun(m)}
    for name in sorted(set(names), key=len, reverse=True):
        start = text.find(name)
        while start >= 0:
            end = start + len(name)
            if not any(taken[start:end]) and (len(name) > 1 or (start, end) in proper):
                spans.append((start, end, None, True))
                taken[start:end] = [True] * (end - start)
            start = text.find(name, end)
    words: list[Word] = []
    used: set[int] = set()
    for s, e, caption, is_name in sorted(spans):
        inside = [i for i, m in enumerate(morphemes) if m.end() > s and m.begin() < e]
        if not inside:
            continue
        lo, hi = morphemes[inside[0]].begin(), morphemes[inside[-1]].end()
        if caption is not None and (lo < s or e < hi):
            around = text[lo:s] + text[e:hi]
            caption = (
                None
                if fa.KANJI_RE.search(around)
                else fa.to_hiragana(text[lo:s]) + caption + fa.to_hiragana(text[e:hi])
            )
        words.append(
            Word(
                line_no,
                lo,
                hi,
                text[lo:hi],
                "".join(fa.to_hiragana(morphemes[i].reading_form()) for i in inside),
                caption,
                ["name"] if is_name else [],
            )
        )
        used.update(inside)
    for i, j in number_spans(morphemes):
        if used.intersection(range(i, j)):
            continue
        lo, hi = morphemes[i].begin(), morphemes[j - 1].end()
        surface = text[lo:hi]
        parts = morphemes[i:j]
        cut = not all(is_numeral(m) or is_counter(m) for m in parts)
        if read is not None and (fa.DIGITS_RE.search(surface) or cut):
            sudachi = fa.to_hiragana(read(surface))
        else:
            sudachi = "".join(fa.to_hiragana(m.reading_form()) for m in parts)
        words.append(Word(line_no, lo, hi, surface, sudachi, None, ["number"]))
        used.update(range(i, j))
    for i, j in split_runs(morphemes, used):
        lo, hi = morphemes[i].begin(), morphemes[j - 1].end()
        sudachi = "".join(fa.to_hiragana(m.reading_form()) for m in morphemes[i:j])
        words.append(Word(line_no, lo, hi, text[lo:hi], sudachi, None, ["split"]))
        used.update(range(i, j))
    for i, m in enumerate(morphemes):
        if i in used or not fa.KANJI_RE.search(m.surface()):
            continue
        word = Word(line_no, m.begin(), m.end(), m.surface(), fa.to_hiragana(m.reading_form()))
        if is_proper_noun(m):
            word.kinds.append("name")
        words.append(word)
    words.sort(key=lambda w: w.start)
    return words


def is_proper_noun(m) -> bool:
    return m.part_of_speech()[1] == "固有名詞"


def is_counter(m) -> bool:
    """Whether a morpheme after a numeral counts it: a suffix (日 of 七日, 人 of 3人) or a noun
    that can be a counter (日 of 10日, 月, 年)."""
    pos = m.part_of_speech()
    return pos[0] == "接尾辞" or tuple(pos[:3]) == ("名詞", "普通名詞", "助数詞可能")


def is_numeral(m) -> bool:
    return tuple(m.part_of_speech()[:2]) == NUMERAL


def number_spans(morphemes: Sequence) -> list[tuple[int, int]]:
    """[i, j) morpheme ranges of the numbers that are one word: a run of numerals and the
    counter after it, if any, where a kanji is among them (10日, 十月, １万). With none, as in
    175 or 3つ, the number has no kanji to give furigana to. A word right after the numerals
    that starts with a numeral kanji is the number's too, with the counter after it if any: in
    a line Sudachi cuts 三十分 into 三|十分, which it reads じゅうぶん, enough."""
    spans = []
    i = 0
    while i < len(morphemes):
        if not is_numeral(morphemes[i]):
            i += 1
            continue
        j = i
        while j < len(morphemes) and is_numeral(morphemes[j]):
            j += 1
        if j < len(morphemes) and morphemes[j].surface()[:1] in NUMERAL_KANJI:
            j += 1
        end = j + 1 if j < len(morphemes) and is_counter(morphemes[j]) else j
        if any(fa.KANJI_RE.search(m.surface()) for m in morphemes[i:end]):
            spans.append((i, end))
        i = j
    return spans


def split_runs(morphemes: Sequence, used: set[int]) -> list[tuple[int, int]]:
    """[i, j) morpheme ranges of the kanji runs that are one word though Sudachi cut them: two
    morphemes of kanji only or more in a row, not taken by another word, each a single kanji
    or one of them unread (its reading still the kanji, which is how Sudachi reads one it
    does not know)."""

    def kanji_only(k: int) -> bool:
        surface = morphemes[k].surface()
        return k not in used and bool(surface) and all(fa.KANJI_RE.match(c) for c in surface)

    spans = []
    i = 0
    while i < len(morphemes):
        if not kanji_only(i):
            i += 1
            continue
        j = i + 1
        while j < len(morphemes) and kanji_only(j):
            j += 1
        run = morphemes[i:j]
        single = all(len(m.surface()) == 1 for m in run)
        unread = any(fa.KANJI_RE.search(m.reading_form()) for m in run)
        if len(run) > 1 and (single or unread):
            spans.append((i, j))
        i = j
    return spans


def numeral_reading(text: str, tokenize: Callable[[str], Sequence]) -> str:
    """Sudachi's reading of `text` with its digits spelt in kanji numerals, hiragana."""
    return "".join(fa.to_hiragana(m.reading_form()) for m in tokenize(fa.kanji_numerals(text)))


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
            line_words = kanji_words(
                n, text, morphemes, readings, names, lambda s: numeral_reading(s, tokenize)
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
    base = ReadingBase(rows, jmdict_readings)
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
