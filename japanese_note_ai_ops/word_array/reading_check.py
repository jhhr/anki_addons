"""Whether a sentence's audio says the readings its furigana draft gives, word by word, and which
reading to write for each.

A transcript of the clip in kana, by a model that never saw the caption, and the line as the
draft reads it are both turned into katakana morae as pronounced: a long vowel mark is the
vowel it lengthens, は/へ/を as particles are ワ/エ/オ, ヂ/ヅ are ジ/ズ. They are aligned by a
weighted edit distance, with a cheap step for the spellings of one sound (オウ and オー, エイ and
エー, a dropped っ or long vowel), and each kanji word gets the stretch of the transcript between
its aligned neighbours (`reference`, `neighbours`). A word is heard as a reading when that
stretch holds the reading, only cheap steps off, and holds no other reading of the word better:
a word heard as another reading pulls the alignment's edges about (猫猫 heard オマオ), so every
reading a word is tested against may move either edge by a mora, at a small cost that lets the
reading covering the most win (婆 heard ババア is ばばあ, not ばあ).

The rule (`decide`) writes the draft where a quorum of the transcripts hears it, a reading the
dictionary has for the spelling or the show has for a name where they hear that instead
(`choices`), and nothing else: a word heard as two readings, or as one nothing licenses, goes to
review. A correction must fit more closely than a confirmation (`CORRECT`), on a stretch never
shrunk to fit, and none is written where a vowel's length is all that tells it from another
licensed reading (`length_sibling`). Each limit came from readings the furigana audio eval
(`research/furigana_audio*.py`) got wrong. That eval scores its transcripts through this
module, so it is this check's regression test: a change here shows in its numbers.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Sequence

from ..kana_conv import to_katakana
from .captions import DIGITS_RE, KANJI_RE
from .kanji_words import numeral_reading

SMALL = "ャュョァィゥェォヮ"
VOWEL_ROWS = {
    "a": "アカサタナハマヤラワガザダバパァャヮ",
    "i": "イキシチニヒミリギジヂビピィ",
    "u": "ウクスツヌフムユルグズヅブプゥュヴ",
    "e": "エケセテネヘメレゲゼデベペェ",
    "o": "オコソトノホモヨロヲゴゾドボポォョ",
}
VOWEL_OF = {ch: v for v, row in VOWEL_ROWS.items() for ch in row}
VOWEL_KANA = {"a": "ア", "i": "イ", "u": "ウ", "e": "エ", "o": "オ"}
PARTICLE_SOUND = {"ハ": "ワ", "ヘ": "エ", "ヲ": "オ"}
SYMBOLS = ("記号", "補助記号", "空白")
# A line whose transcript is off it by more than this share of morae, its words read as the
# transcript hears them (`line_up`), is taken for one whose caption and clip differ: the
# caption holds text the clip cuts off, or lines spoken over each other
MISMATCH = 0.35
# Two readings agree when the steps between them cost no more than this: cheap ones only
AGREE = 0.45
CHEAP = 0.2


def vowel(mora: str) -> str:
    return VOWEL_OF.get(mora[-1], "") if mora else ""


def morae(kana: str) -> list[str]:
    """Katakana morae of the kana in `kana`; everything else is dropped. A small kana joins the
    mora before it, a long vowel mark becomes the vowel it lengthens."""
    out: list[str] = []
    for ch in to_katakana(kana).replace("ヂ", "ジ").replace("ヅ", "ズ").replace("ヲ", "オ"):
        if ch == "ー":
            if out and vowel(out[-1]):
                out.append(VOWEL_KANA[vowel(out[-1])])
        elif ch in SMALL and out:
            out[-1] += ch
        elif "ァ" <= ch <= "ヺ":
            out.append(ch)
    return out


def step(a: str, b: str, before: str) -> float:
    """What replacing mora `a` (after `before`) by `b` costs."""
    if a == b:
        return 0.0
    if {a, b} <= {"ウ", "オ"} and vowel(before) == "o":
        return CHEAP
    if {a, b} <= {"イ", "エ"} and vowel(before) == "e":
        return CHEAP
    return 0.8 if vowel(a) and vowel(a) == vowel(b) else 1.0


def lengthens(m: str, before: str) -> bool:
    """Whether vowel mora `m` only lengthens the vowel before it: アア, オウ, エイ and the like,
    which speech shortens and spelling writes either way (以上 heard イジョ)."""
    v = vowel(before)
    return bool(v) and (m == VOWEL_KANA[v] or (m, v) in {("ウ", "o"), ("イ", "e")})


def gap(m: str, before: str) -> float:
    """What a mora missing on one side costs: little for a geminate or a lengthening vowel,
    which speech and spelling drop and add freely, and a bit more than a wrong mora otherwise,
    so that a word heard as another of its length lines up mora by mora (桜花 オウカ heard
    インファ)."""
    if m == "ッ" or lengthens(m, before):
        return CHEAP
    return 1.05


def align(ref: Sequence[str], hyp: Sequence[str]) -> tuple[float, list[Optional[int]]]:
    """The edit distance and, for each ref mora, the hyp mora aligned to it (None for a gap)."""
    n, m = len(ref), len(hyp)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[int]] = [[0] * (m + 1) for _ in range(n + 1)]  # 1 sub, 2 del, 3 ins
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            here = cost[i][j]
            if here == inf:
                continue
            if i < n and j < m:
                c = here + step(ref[i], hyp[j], ref[i - 1] if i else "")
                if c < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = c, 1
            if i < n:
                c = here + gap(ref[i], ref[i - 1] if i else "")
                if c < cost[i + 1][j]:
                    cost[i + 1][j], back[i + 1][j] = c, 2
            if j < m:
                c = here + gap(hyp[j], hyp[j - 1] if j else "")
                if c < cost[i][j + 1]:
                    cost[i][j + 1], back[i][j + 1] = c, 3
    pairs: list[Optional[int]] = [None] * n
    i, j = n, m
    while i or j:
        move = back[i][j]
        if move == 1:
            i, j = i - 1, j - 1
            pairs[i] = j
        elif move == 2:
            i -= 1
        else:
            j -= 1
    return cost[n][m], pairs


def agrees(a: str, b: str) -> bool:
    """Whether two readings sound alike to a transcript: cheap steps apart at most, so a long
    vowel or a geminate either way."""
    return align(morae(a), morae(b))[0] <= AGREE


def sound(kana: str) -> tuple[str, ...]:
    """The morae as said, オウ and エイ as the long vowels they are, so that とうきょう and
    トーキョー are one sound and ほんと and ほんとう two."""
    out: list[str] = []
    for m in morae(kana):
        before = vowel(out[-1]) if out else ""
        if (m, before) in {("ウ", "o"), ("イ", "e")}:
            m = VOWEL_KANA[before]
        out.append(m)
    return tuple(out)


def same_reading(a: str, b: str) -> bool:
    """Whether two readings are one as furigana: the same morae as said, however spelled
    (せんせい and センセー), and vowel length counts (婆 ばば and ばばあ, ほんと and ほんとう).
    `agrees` lets a length pass because a transcript is unsure of it; a written reading is
    judged against the one said by this."""
    return sound(a) == sound(b)


# What moving either end of the stretch by a mora costs a reading: less than any real step,
# enough that a reading covering the whole stretch beats one inside it (ばばあ over ばあ)
SHIFT = 0.1


def fit(reading: str, hyp: Sequence[str], lo: int, hi: int, inward: bool = True) -> float:
    """How far `reading` is from hyp[lo:hi], or from a stretch a mora longer at either end
    and, with `inward`, a mora shorter, each such move costing SHIFT."""
    want = morae(reading)
    if not want:
        return float("inf")
    best = float("inf")
    last_a = lo + 1 if inward else lo
    first_b = hi - 1 if inward else hi
    for a in range(max(0, lo - 1), min(len(hyp), last_a + 1)):
        for b in range(max(a + 1, first_b), min(len(hyp), hi + 1) + 1):
            moved = SHIFT * (abs(a - lo) + abs(b - hi))
            best = min(best, align(want, hyp[a:b])[0] + moved)
    return best


# A reading other than the draft is written only on a closer fit than the one that confirms
# the draft: one cheap step at most, and never on a stretch shrunk to fit, which leaves a mora
# heard in the word's place unexplained. 一 of 一期生, heard イッ, fit the イー JMdict also has
# a long vowel and an edge move away, and 匹 of 一匹, heard ピキ, its き inside the stretch;
# each was written so three times
CORRECT = CHEAP


def hears(
    readings: dict[str, str], hyp: Sequence[str], lo: int, hi: int, strict: Iterable[str] = ()
) -> dict[str, bool]:
    """For each named reading of a word, whether the transcript holds it there: within AGREE,
    or CORRECT on a stretch never shrunk for the `strict` ones, and no further than the
    reading that fits best, since a draft read ばあ fits inside the ババア heard for ばばあ."""
    strict = set(strict)
    fits = {key: fit(r, hyp, lo, hi, inward=key not in strict) for key, r in readings.items()}
    best = min(fits.values())
    limit = {key: CORRECT if key in strict else AGREE for key in readings}
    return {key: f <= limit[key] + 1e-9 and f <= best + 1e-9 for key, f in fits.items()}


def spoken(mo: Any) -> list[str]:
    """A morpheme's morae as pronounced: its reading, a particle は/へ/を as ワ/エ/オ. A space,
    a bracket or ♪ is no sound, though Sudachi reads it キゴウ ("symbol"), which put three
    morae no one says into the line at every one."""
    surface = mo.surface()
    pos = mo.part_of_speech()[0]
    if pos in SYMBOLS:
        return morae(surface)
    if pos == "助詞" and to_katakana(surface) in PARTICLE_SOUND:
        return [PARTICLE_SOUND[to_katakana(surface)]]
    return morae(mo.reading_form())


class Span(NamedTuple):
    """A kanji word as its line is aligned: its line, where it is in the line without inline
    readings, and the reading it is read with there (the captions', else the draft's)."""

    line: int
    start: int
    end: int
    reading: str


class Reference(NamedTuple):
    """Lines as said, their words' morae marked (`reference`)."""

    morae: list[str]
    # Which word each mora belongs to, as a place in `placed`; -1 for none
    owner: list[int]
    # The indices into the spans of the words found in their lines, in order
    placed: list[int]
    # Where each placed word's morae start: a word whose reading has no kana (each kanji of it
    # unread, 療所) has none, and is found by this (`neighbours`)
    starts: list[int]


def reference(
    texts: Sequence[str], spans: Sequence[Span], tokenize: Callable[[str], Sequence]
) -> Reference:
    """The lines' morae as said, and where each word's are. A word is read as its span's
    reading from the morpheme it starts at; one that does not start at a morpheme is not found.
    The rest of a line is read as Sudachi reads it, particles as pronounced, and digits no word
    took as a number (175), not digit by digit. A morpheme of no text is skipped and a word
    placed once: Sudachi reads … as ... and puts the two dots it adds where the next word
    starts, which placed the word three times. `texts` are the lines without their inline
    readings."""
    ref: list[str] = []
    owner: list[int] = []
    placed: list[int] = []
    starts: list[int] = []
    by_line: dict[int, list[int]] = defaultdict(list)
    for k, span in enumerate(spans):
        by_line[span.line].append(k)
    for n, text in enumerate(texts):
        mine = sorted(by_line.get(n, []), key=lambda k: spans[k].start)
        i = 0
        for mo in tokenize(text):
            if mo.end() <= mo.begin():
                continue
            while i < len(mine) and spans[mine[i]].end <= mo.begin():
                i += 1
            if i < len(mine) and spans[mine[i]].start <= mo.begin() < spans[mine[i]].end:
                if mo.begin() == spans[mine[i]].start and placed[-1:] != [mine[i]]:
                    placed.append(mine[i])
                    starts.append(len(ref))
                    got = morae(spans[mine[i]].reading)
                    ref.extend(got)
                    owner.extend([len(placed) - 1] * len(got))
                continue
            got = (
                morae(numeral_reading(mo.surface(), tokenize))
                if DIGITS_RE.search(mo.surface())
                else spoken(mo)
            )
            ref.extend(got)
            owner.extend([-1] * len(got))
    return Reference(ref, owner, placed, starts)


def neighbours(
    owner: Sequence[int],
    pairs: Sequence[Optional[int]],
    k: int,
    n: int,
    start: Optional[int] = None,
) -> tuple[int, int]:
    """The stretch of the transcript between the last mora aligned before word `k` and the
    first aligned after it. A word with no morae is found by `start`, where they would start
    (`Reference.starts`); without it, it has no stretch, (0, 0)."""
    mine = [i for i, o in enumerate(owner) if o == k]
    if mine:
        first, last = mine[0], mine[-1]
    elif start is not None:
        first, last = start, start - 1
    else:
        return 0, 0
    aligned = [(i, j) for i, j in enumerate(pairs) if j is not None]
    before = [j for i, j in aligned if i < first]
    after = [j for i, j in aligned if i > last]
    lo = before[-1] + 1 if before else 0
    hi = after[0] if after else n
    return lo, max(lo, hi)


def choices(draft: str, others: Iterable[str]) -> dict[str, str]:
    """The readings the op may write for a word, by key: "draft" for Sudachi's, each of
    `others` (the dictionary's for the spelling, the show's for a name; never the line's own
    caption reading, which needs no audio) by its kana. An other counts only where it is not
    the draft's own sound, a long vowel or a dropped っ away, which would be the draft heard;
    the draft does not count where Sudachi had no reading for one of its kanji (苓 of 翠苓),
    however well its kana fit, and then rules out no other: 趙迂 drafted ちょう迂 sounds like
    the ちょうう the show's list has for it, which is no reason to drop that."""
    if KANJI_RE.search(draft):
        return {other: other for other in others}
    options = {"draft": draft}
    for other in others:
        options.setdefault(other, other)
    return {key: r for key, r in options.items() if key == "draft" or not agrees(r, draft)}


def hears_choices(
    options: Mapping[str, str], hyp: Sequence[str], lo: int, hi: int
) -> dict[str, bool]:
    """Whether a transcript holds each of a word's `choices` at the word, the others held to a
    correction's limits (`hears`' strict); "draft" is always answered, False where it is no
    choice."""
    others = [key for key in options if key != "draft"]
    heard = hears(dict(options), hyp, lo, hi, strict=others) if options else {}
    return {"draft": False, **heard}


def heard_reading(options: Mapping[str, str], heard: Mapping[str, bool]) -> Optional[str]:
    """The reading a transcript hears a word as, of its `choices` (`hears_choices`' answer):
    the draft where it hears that, else the first other it hears; None where it hears none."""
    if heard.get("draft"):
        return options["draft"]
    others = sorted(key for key, yes in heard.items() if yes and key != "draft")
    return options[others[0]] if others else None


def over_limit(cost: float, ref: Sequence[str]) -> bool:
    return cost > MISMATCH * max(len(ref), 1)


def line_up(
    texts: Sequence[str],
    spans: Sequence[Span],
    lines: Reference,
    hyp: Sequence[str],
    options: Mapping[int, Mapping[str, str]],
    tokenize: Callable[[str], Sequence],
) -> tuple[Reference, list[Optional[int]], bool]:
    """How a transcript lines up with its `lines` (`reference` of `spans`): the lines as read
    and the transcript's mora aligned to each of their morae, which each word is heard by
    (`neighbours`), and whether the transcript is far from them (MISMATCH).

    The lines are read with Sudachi's drafts, which the op is there to correct, and on a short
    line one wrong draft is enough to put a transcript over the limit (何時に起きた heard
    ナンジニオキタ, drafted いつ). So a transcript over it is aligned again with each word read
    as it hears it, of the word's `choices` (`options`, by index into `spans`; none for a word
    the captions read): it is far only if it is still over, and otherwise its words are heard
    on the second alignment, where a word read wrong no longer pulls its neighbours' morae."""
    cost, pairs = align(lines.morae, hyp)
    if not over_limit(cost, lines.morae):
        return lines, pairs, False
    refit = list(spans)
    for k, i in enumerate(lines.placed):
        if i in options:
            lo, hi = neighbours(lines.owner, pairs, k, len(hyp), lines.starts[k])
            reading = heard_reading(options[i], hears_choices(options[i], hyp, lo, hi))
            if reading is not None:
                refit[i] = refit[i]._replace(reading=reading)
    again = reference(texts, refit, tokenize)
    again_cost, again_pairs = align(again.morae, hyp)
    if over_limit(again_cost, again.morae):
        return lines, pairs, True
    return again, again_pairs, False


def length_sibling(reading: str, readings: Iterable[str]) -> bool:
    """Whether one of `readings` differs from `reading` only in what a transcript is unsure
    of, a long vowel or a っ: it `agrees` and is not the `same_reading`."""
    return any(agrees(r, reading) and not same_reading(r, reading) for r in readings)


def decide(
    hearings: Sequence[Mapping[str, bool]],
    draft: str,
    quorum: int,
    licensed: Iterable[str] = (),
) -> Optional[str]:
    """The reading the op writes for a word, given what each transcript hears of its choices
    (`hears_choices`), or None for a review: the draft where a quorum hears it, another reading
    where a quorum hears that one; never when a quorum hears each of two readings.

    Nor another reading while one of the word's `licensed` readings (the `others` its choices
    were made of) is a `length_sibling` of it. The models drop a final long vowel: 婆 said
    ばばあ was heard ババ by both, and JMdict reads it ばば too, which the rule wrote, three
    times in the eval with 爺 じじ for じじい. Kept to corrections: a draft confirmed with such
    a sibling (本当 ほんとう, ほんと) was never found wrong, and holding those back sent five
    times as many words to review, on ordinary lines too."""

    def votes(key: str) -> int:
        return sum(h.get(key, False) for h in hearings)

    heard_draft = votes("draft") >= quorum
    heard = {key for h in hearings for key, yes in h.items() if yes and key != "draft"}
    others = {key for key in heard if votes(key) >= quorum}
    if heard_draft and not others:
        return draft
    if not heard_draft and others:
        first = sorted(others)[0]
        if all(agrees(first, key) for key in others) and not length_sibling(first, licensed):
            return first
    return None
