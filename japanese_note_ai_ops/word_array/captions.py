"""A sentence taken from a show's Japanese closed captions, as spoken: what a subtitle line says
and the readings the captions give.

A sentence note made from subtitles (subs2srs) holds the caption as the show wrote it, which
differs from what is said in ways furigana set from the audio has to know about:
- a speaker label opens a speaker's part, `（猫猫）ん？`, and a sound description stands alone,
  `（赤ん坊のはしゃぎ声）`. Neither is spoken, so both are dropped, full-width parentheses and
  all;
- a reading follows a word in half-width parentheses, `猫猫(マオマオ)` or `緑青館(ろくしょうかん)`,
  usually only at the word's first mention in an episode, sometimes inside a label
  (`（羅門(ルォメン)）`). It is the caption writer's reading, so it is kept as the word's
  furigana. The word can follow other kanji, `白鈴姐(ねえ)` or `鈴麗公主(ひめ)`, so which of them
  the reading is for is a guess (`split_readings`' `base`);
- ASS override tags (`{\\an8}`) mark a line drawn at the top of the screen while another one
  runs at the bottom. subs2srs merged such overlapping events into one card and repeated
  their text, so the card's text and its audio no longer match.

A field is HTML. A caption should come without markup but may hold some: subs2srs keeps an
SRT's <font> colors and takes its line separator from a setting (<br /> as well as <br>), and
Anki's editor adds <span style> and writes lines as <div> blocks. Sudachi reads a tag's name as
words (フォント カラー), so a caption is read as plain text, its line breaks kept
(`caption_lines`), and the furigana written from it holds no other markup.

Each of these turned up in the furigana audio eval's show (`research/furigana_audio*.py`), which
reads its captions through this module.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Callable, Iterable, NamedTuple

from ..html_stripping import text_lines
from ..kana_conv import to_hiragana

KANJI = "一-鿿㐀-䶿々〆ヶ"
KANJI_RE = re.compile(f"[{KANJI}]")
# A reading the captions give in half-width parentheses right after the kanji it reads
INLINE_READING_RE = re.compile(f"([{KANJI}]+)\\(([ぁ-ゖァ-ヺー・]+)\\)")
# Labels and sound descriptions; the half-width parentheses of a reading inside a label are no
# delimiter here, so （羅門(ルォメン)） is one segment
UNSPOKEN_RE = re.compile(r"（[^（）]*）")
ASS_TAG_RE = re.compile(r"\{\\[^}]*\}")
KATAKANA_RE = re.compile(r"[ァ-ヺー・]+")
DIGITS_RE = re.compile(r"[0-9０-９]+")
KANJI_DIGITS = "〇一二三四五六七八九"


def kanji_number(n: int) -> str:
    """`n` in kanji numerals as a word spells it: 10 十, 2010 二千十, 10000 一万."""
    if n == 0 or n >= 10**16:
        return "".join(KANJI_DIGITS[int(d)] for d in str(n))
    out = []
    for size, unit in ((10**12, "兆"), (10**8, "億"), (10**4, "万"), (1, "")):
        group = n // size % 10000
        if not group:
            continue
        for place, name in ((1000, "千"), (100, "百"), (10, "十")):
            digit = group // place % 10
            if digit:
                out.append(("" if digit == 1 else KANJI_DIGITS[digit]) + name)
        if group % 10:
            out.append(KANJI_DIGITS[group % 10])
        out.append(unit)
    return "".join(out)


def kanji_numerals(text: str) -> str:
    """`text` with each run of digits, half or full width, spelt in kanji numerals, the form
    a number is read and looked up in: Sudachi reads 10 digit by digit, イチレイ, and JMdict
    has 十日 (とおか) but no 10日."""
    return DIGITS_RE.sub(lambda m: kanji_number(int(m.group())), text)


def is_noisy(jp: str) -> bool:
    """A card whose text holds overlapping caption events, merged and repeated: its audio
    says each once, so the text is no transcript of it."""
    return ASS_TAG_RE.search(jp) is not None


def caption_lines(jp: str) -> list[str]:
    """The card's caption lines as plain text: no ASS tags, and of its HTML only the line
    breaks (`html_stripping.text_lines`)."""
    return text_lines(ASS_TAG_RE.sub("", jp))


def spoken_lines(jp: str) -> list[str]:
    """The card's caption lines with what is not spoken taken out: ASS tags, HTML, speaker
    labels and sound descriptions. Inline readings stay, `猫猫(マオマオ)`; a line left empty is
    dropped."""
    out = []
    for line in caption_lines(jp):
        line = UNSPOKEN_RE.sub("", line).strip(" 　")
        if line:
            out.append(line)
    return out


class InlineReading(NamedTuple):
    surface: str  # the kanji the reading belongs to
    reading: str  # in hiragana; the captions write names in katakana
    katakana: bool  # whether the caption wrote it in katakana, which it does for names
    start: int  # where the surface starts in the line with the readings removed
    end: int


# How many kanji at the end of the run before a reading the reading is for. A caption puts the
# reading after its word, which may follow other kanji: 白鈴姐(ねえ) reads only 姐
BaseFinder = Callable[[str, str], int]


def whole_run(run: str, reading: str) -> int:
    return len(run)


def split_readings(line: str, base: BaseFinder = whole_run) -> tuple[str, list[InlineReading]]:
    """The line without its inline readings, and each reading at its surface's place there:
    the last `base(run, reading)` kanji of the kanji run before it."""
    out: list[str] = []
    readings: list[InlineReading] = []
    pos = 0
    length = 0
    for m in INLINE_READING_RE.finditer(line):
        upto_run = line[pos : m.end(1)]
        out.append(upto_run)
        length += len(upto_run)
        run, given = m.group(1), m.group(2)
        reading = to_hiragana(given)
        n = max(1, min(len(run), base(run, reading)))
        readings.append(
            InlineReading(
                run[len(run) - n :],
                reading,
                KATAKANA_RE.fullmatch(given) is not None,
                length - n,
                length,
            )
        )
        pos = m.end()
    out.append(line[pos:])
    return "".join(out), readings


def all_inline_readings(jp: str, base: BaseFinder = whole_run) -> list[InlineReading]:
    """Every inline reading of a card, labels' included: a name read in a label (（羅門(ルォメン)）)
    is read so wherever it is spoken."""
    found: list[InlineReading] = []
    for line in caption_lines(jp):
        found.extend(split_readings(line, base)[1])
    return found


def show_readings(
    texts: Iterable[str], base: BaseFinder = whole_run
) -> tuple[dict[str, str], set[str]]:
    """What a show's captions read, from all of them: the surfaces they read in katakana, its
    names, each with the reading given most (the first given, of two given as often), and the
    surfaces they read in hiragana, words the caption writer thought needed a reading."""
    names: dict[str, Counter] = defaultdict(Counter)
    words: set[str] = set()
    for text in texts:
        for r in all_inline_readings(text, base):
            if r.katakana:
                names[r.surface][r.reading] += 1
            else:
                words.add(r.surface)
    return {surface: c.most_common(1)[0][0] for surface, c in names.items()}, words


class ReadingBase:
    """A `BaseFinder` for one show, which knows its captions: which tail of a kanji run an
    inline reading is for, tried in turn:

    1. the longest tail the dictionary reads so (高級妓楼(ぎろう) is 妓楼, 元嫁姑(しゅうとめ) 姑);
    2. the shortest the captions read so elsewhere standing alone (鈴麗公主(ひめ) is 公主, read
       so twice on its own);
    3. what follows the longest head the captions read elsewhere on its own, a name mostly
       (白鈴姐(ねえ) is 姐, 白鈴 being read ぱいりん);
    4. the longest with a kana of reading for each kanji at least (好奇心旺盛(おうせい) cannot be
       more than four kanji);
    and the whole run when none fits. 義父上(ちちうえ) comes out 父上, though the 義 is not said
    either: a guess the audio would correct.
    """

    def __init__(self, texts: Iterable[str], dictionary: Callable[[str], set[str]]):
        """`texts`: the show's captions, each a card's text as written; `dictionary`: a
        spelling's readings."""
        self.alone: dict[str, set[str]] = defaultdict(set)
        for text in texts:
            for r in all_inline_readings(text):
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
