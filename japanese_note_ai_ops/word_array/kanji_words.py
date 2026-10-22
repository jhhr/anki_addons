"""The kanji words of a spoken line, each with the reading Sudachi drafts for it: the units in
which furigana is checked against the audio and written.

A word is a Sudachi morpheme with kanji in it, or several morphemes taken as one where reading
them apart would be wrong:
- an inline reading of the captions (`captions.split_readings`) spans the kanji it reads;
- a name of the show's list, wherever it is written. The captions read a name at its first
  mention in an episode only, so most mentions have no reading, and Sudachi reads them as
  common words (猫猫 ねこねこ, 高順 たかのぶ). A one-kanji name is matched only where Sudachi
  also takes it for a proper noun: 馬 is a family name read まー and also the horse;
- a number with the counter after it, in kanji or in digits (10日, 七日, 一匹, １万, 3つ, 175):
  its reading belongs to the whole and seldom divides by kanji (とおか, なのか, いっぴき). One
  in digits is a word as one in kanji is, so that its furigana says how it is read, which word
  extraction goes by. A number with digits is read as Sudachi reads it spelt in kanji numerals
  (`captions.kanji_numerals`): it reads 10 digit by digit. A number Sudachi cut into a numeral
  and another word (三|十分, さん|じゅうぶん, enough) is read by itself, where Sudachi reads
  三十|分;
- a kanji run Sudachi cut into single kanji (神|美, 羅|半) or into pieces one of which it could
  not read (響|迂): a name, mostly, which the captions never read. Kanji by kanji its parts get
  readings of their own (神 かみ), and an unread one has no sound, so the audio of the whole
  would go to its neighbour. A run cut into longer pieces stays cut (物置|小屋, 柘榴|宮): the
  dictionary knows those pieces. So do words of a kanji each that only meet because the
  particle between them was dropped (誰|来た, 今|私が): a pronoun, a verb and a noun of time
  stand alone (`joined`).

Each rule came from the furigana audio eval (`research/furigana_audio*.py`), whose labelling
asked for a reading piece by piece until the words were taken whole. The morphemes are
SudachiPy's, or anything with its `surface()`, `reading_form()`, `part_of_speech()`, `begin()`
and `end()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from ..kana_conv import to_hiragana
from .captions import DIGITS_RE, KANJI_RE, LATIN_RE, InlineReading, kanji_numerals
from .numbers import DIGITS, LARGE_UNITS, SMALL_UNITS

# Sudachi's part of speech for a numeral
NUMERAL = ("名詞", "数詞")
# The kanji a number is spelt with, as `captions.kanji_number` writes it
NUMERAL_KANJI = frozenset(DIGITS) | frozenset(SMALL_UNITS) | frozenset(LARGE_UNITS)
# Sudachi's part of speech for a noun that can be used as an adverb, of time mostly (今, 時)
ADVERBIAL_NOUN = ("名詞", "普通名詞", "副詞可能")


@dataclass
class Word:
    """A kanji word of a spoken line, or a number in digits: a Sudachi morpheme, or several
    where a name, an inline reading or a number spans them. `start` and `end` are offsets in
    the line without its inline readings."""

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


def kanji_words(
    line_no: int,
    text: str,
    morphemes: Sequence,
    readings: Sequence[InlineReading],
    names: Iterable[str],
    read: Optional[Callable[[str], str]] = None,
) -> list[Word]:
    """The line's kanji words: one per kanji morpheme, the morphemes an inline reading or a
    name of `names` spans merged into one word, and so are those of a number (`number_spans`).
    A span the tokenizer cut across takes in the whole of each morpheme it touches, so the word
    may run past it (玉葉|妃 for 玉葉), and spans touching one morpheme are one word, read as
    their readings in a row (徳妃(とくひ)里樹(リーシュ), cut 徳|妃里|樹, is とくひりーしゅ). An
    inline reading covers its kanji only, 噛(か)みつい, so the word's caption takes in the kana
    around them as written (かみつい); a word with other kanji around them gets none. `read`
    gives the reading of a number with digits, which Sudachi reads digit by digit, and of one
    it cut into a numeral and another word (三|十分, じゅうぶん); without it the morphemes'
    readings are joined. Last, a kanji run Sudachi cut where it should not have
    (`split_runs`) is one word."""
    spans: list[tuple[int, int, Optional[str], bool]] = []  # start, end, caption, is_name
    for r in readings:
        spans.append((r.start, r.end, r.reading, r.katakana))
    taken = [False] * len(text)
    for s, e, _, _ in spans:
        taken[s:e] = [True] * (e - s)
    proper = {(m.begin(), m.end()) for m in morphemes if is_proper_noun(m)}
    # The longest first, and of two as long the first in code point order, whatever order the
    # set gives, which changes from one session to the next (李白鈴 is 李白 and 鈴, never 李 and
    # 白鈴). An empty name is found everywhere, and never past where it was found: skipped
    for name in sorted(set(names) - {""}, key=lambda name: (-len(name), name)):
        start = text.find(name)
        while start >= 0:
            end = start + len(name)
            if not any(taken[start:end]) and (len(name) > 1 or (start, end) in proper):
                spans.append((start, end, None, True))
                taken[start:end] = [True] * (end - start)
            start = text.find(name, end)
    # The spans by the morphemes they touch, [first, last]; spans touching one morpheme are
    # one word, which their readings read together (徳妃(とくひ)里樹(リーシュ), cut 徳|妃里|樹)
    groups: list[tuple[int, int, list[tuple[int, int, Optional[str], bool]]]] = []
    for span in sorted(spans):
        s, e = span[0], span[1]
        inside = [i for i, m in enumerate(morphemes) if m.end() > s and m.begin() < e]
        if not inside:
            continue
        if groups and inside[0] <= groups[-1][1]:
            first, last, members = groups[-1]
            groups[-1] = (first, max(last, inside[-1]), members + [span])
        else:
            groups.append((inside[0], inside[-1], [span]))
    words: list[Word] = []
    used: set[int] = set()
    for first, last, members in groups:
        lo, hi = morphemes[first].begin(), morphemes[last].end()
        words.append(
            Word(
                line_no,
                lo,
                hi,
                text[lo:hi],
                "".join(to_hiragana(m.reading_form()) for m in morphemes[first : last + 1]),
                joined_caption(text, lo, hi, members),
                ["name"] if any(is_name for *_, is_name in members) else [],
            )
        )
        used.update(range(first, last + 1))
    for i, j in number_spans(morphemes):
        if used.intersection(range(i, j)):
            continue
        lo, hi = morphemes[i].begin(), morphemes[j - 1].end()
        surface = text[lo:hi]
        parts = morphemes[i:j]
        cut = not all(is_numeral(m) or is_counter(m) for m in parts)
        if read is not None and (DIGITS_RE.search(surface) or cut):
            sudachi = to_hiragana(read(surface))
        else:
            sudachi = "".join(to_hiragana(m.reading_form()) for m in parts)
        words.append(Word(line_no, lo, hi, surface, sudachi, None, ["number"]))
        used.update(range(i, j))
    for i, j in split_runs(morphemes, used):
        lo, hi = morphemes[i].begin(), morphemes[j - 1].end()
        sudachi = "".join(to_hiragana(m.reading_form()) for m in morphemes[i:j])
        words.append(Word(line_no, lo, hi, text[lo:hi], sudachi, None, ["split"]))
        used.update(range(i, j))
    for i, m in enumerate(morphemes):
        if i in used or not KANJI_RE.search(m.surface()):
            continue
        word = Word(line_no, m.begin(), m.end(), m.surface(), to_hiragana(m.reading_form()))
        if is_proper_noun(m):
            word.kinds.append("name")
        words.append(word)
    words.sort(key=lambda w: w.start)
    return words


def joined_caption(
    text: str, lo: int, hi: int, spans: Sequence[tuple[int, int, Optional[str], bool]]
) -> Optional[str]:
    """The captions' reading of the word text[lo:hi]: its spans' readings in order, with the
    kana around and between them as written; none where a span has no reading (a name of the
    list) or a kanji is outside the spans."""
    pieces: list[str] = []
    pos = lo
    for s, e, caption, _ in spans:
        if caption is None or KANJI_RE.search(text[pos:s]):
            return None
        pieces += [to_hiragana(text[pos:s]), caption]
        pos = e
    if KANJI_RE.search(text[pos:hi]):
        return None
    return "".join(pieces) + to_hiragana(text[pos:hi])


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
    counter after it, if any, where a kanji or a digit is among them (10日, 十月, １万, 175,
    3つ); a number in kana needs no furigana. A counter in Latin letters is no part of it,
    since a letter is given no furigana: 100[ひゃく]km. A word right after the numerals that
    starts with a numeral kanji is the number's too, with the counter after it if any: in a
    line Sudachi cuts 三十分 into 三|十分, which it reads じゅうぶん, enough."""
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
        counted = (
            j < len(morphemes)
            and is_counter(morphemes[j])
            and not LATIN_RE.search(morphemes[j].surface())
        )
        end = j + 1 if counted else j
        surfaces = "".join(m.surface() for m in morphemes[i:end])
        if KANJI_RE.search(surfaces) or DIGITS_RE.search(surfaces):
            spans.append((i, end))
        i = j
    return spans


def joined(a, b) -> bool:
    """Whether kanji morphemes `a` and `b`, in a row, can be pieces of one word Sudachi cut.
    Not where one of them is a word that stands alone in the line, a kanji each because the
    particle between them was dropped (誰|来た, 今|私が, 間|机の): a pronoun, which takes a
    suffix at most (何|様); a verb, which only a prefix comes before (片|喰) or a suffix after
    (出|方) in a word; a noun of time used as an adverb (今, 時, 間, 晩), which nothing
    follows in a word Sudachi would cut (父|上 is Sudachi's うえ after it)."""
    pa, pb = a.part_of_speech(), b.part_of_speech()
    if pb[0] == "代名詞" or (pa[0] == "代名詞" and pb[0] != "接尾辞"):
        return False
    if (pb[0] == "動詞" and pa[0] != "接頭辞") or (pa[0] == "動詞" and pb[0] != "接尾辞"):
        return False
    return tuple(pa[:3]) != ADVERBIAL_NOUN


def split_runs(morphemes: Sequence, used: set[int]) -> list[tuple[int, int]]:
    """[i, j) morpheme ranges of the kanji runs that are one word though Sudachi cut them: two
    morphemes of kanji only or more in a row, not taken by another word, each a single kanji
    or one of them unread (its reading still the kanji, which is how Sudachi reads one it
    does not know). Such a run is cut where a piece is not `joined` to the one before it, and
    each part of two pieces or more that is such a run by itself is one word."""

    def kanji_only(k: int) -> bool:
        surface = morphemes[k].surface()
        return k not in used and bool(surface) and all(KANJI_RE.match(c) for c in surface)

    def cut_wrongly(run: Sequence) -> bool:
        single = all(len(m.surface()) == 1 for m in run)
        unread = any(KANJI_RE.search(m.reading_form()) for m in run)
        return len(run) > 1 and (single or unread)

    spans = []
    i = 0
    while i < len(morphemes):
        if not kanji_only(i):
            i += 1
            continue
        j = i + 1
        while j < len(morphemes) and kanji_only(j):
            j += 1
        if cut_wrongly(morphemes[i:j]):
            start = i
            for k in range(i + 1, j + 1):
                if k == j or not joined(morphemes[k - 1], morphemes[k]):
                    if cut_wrongly(morphemes[start:k]):
                        spans.append((start, k))
                    start = k
        i = j
    return spans


def numeral_reading(text: str, tokenize: Callable[[str], Sequence]) -> str:
    """Sudachi's reading of `text` with its digits spelt in kanji numerals, hiragana."""
    return "".join(to_hiragana(m.reading_form()) for m in tokenize(kanji_numerals(text)))
