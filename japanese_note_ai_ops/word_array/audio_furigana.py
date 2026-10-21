"""Furigana for a sentence from its audio: the caption's kanji words read as the clip says.

A sentence mined from a show's captions (`captions.py`) is drafted by Sudachi word by word
(`kanji_words.py`), and each word's draft is checked against the kana transcripts of its clip
(`reading_check.py`). A word is written as the transcripts hear it: the draft, or another
reading the dictionary has for its spelling or the show's list has for a name. Anything else
goes to review. A word the captions read themselves (猫猫(マオマオ)) is written as they read
it, which needs no audio. A transcript far from its caption (one the clip cuts off, lines spoken
over each other, a caption repeating the text of events subs2srs merged) is no evidence for any
word of the sentence, so all of them go to review. Far means far with each word read as the
transcript hears it (`reading_check.line_up`): on a short line one wrong draft is enough else.

A word in review is written with the readings to choose between in its brackets, joined by ？
(`DOUBT`): its draft, then what else the transcripts heard there (`Reading.offered`),
方[ほう？かた], and a lone one followed by ？, 薬[くすり？]. So the note itself says which word
is in doubt, and a person fixing it confirms each such word by editing it: kanjify and word
extraction leave a sentence holding one alone (`has_doubt`) even once its review tag is gone.

Speaker labels and sound descriptions are not spoken, so they are left out of the furigana
(captions.spoken_lines), as is the caption's HTML but for its line breaks, and a word whose
draft holds a kanji Sudachi could not read gets no furigana until someone gives it one: none is
better than a wrong one.

A transcript is read as kana: anything else in it is dropped, so a model writing ruby text
(漢字[かな]) gives its readings, and one writing ordinary text gives too little to check by.
Run over the furigana audio eval's cards with its two runs as the transcripts, this decides
every word as the eval's rule does (`research/furigana_audio_score.py --combine`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from typing import Callable, Container, Iterable, Mapping, Optional, Sequence

from ..kana_conv import to_hiragana
from ..shared.jp_text_processing.kana.make_furigana_from_reading import (
    make_furigana_from_reading,
)
from .captions import (
    KANJI_RE,
    LATIN_RE,
    BaseFinder,
    kanji_numerals,
    read_letters,
    spoken_lines,
    split_readings,
    whole_run,
)
from .kanji_words import Word, kanji_words, numeral_reading
from .reading_check import (
    Span,
    agrees,
    choices,
    decide,
    hears_choices,
    line_up,
    morae,
    neighbours,
    reference,
    spelt_as,
)

# A spelling's readings in the dictionary
Dictionary = Callable[[str], Iterable[str]]

# Joins the readings a word in review is written with; no reading holds it
DOUBT = "？"
DOUBT_RE = re.compile(r"\[[^\]]*" + DOUBT + r"[^\]]*\]")
# Katakana at the end of a word: a counter, 十センチ
KATAKANA_TAIL_RE = re.compile(r"[ァ-ヺー]+$")
# A word as the Latin letters at its start, the rest, and those at its end: Ｂ型 is Ｂ, 型
LETTERS_AROUND_RE = re.compile(r"([A-Za-zＡ-Ｚａ-ｚ]*)(.*?)([A-Za-zＡ-Ｚａ-ｚ]*)", re.S)
# make_furigana_from_reading's furigana of a word that is one group: kana before it, the group,
# kana after it (お 茶[ちゃ], 開[ひら]か)
ONE_GROUP_RE = re.compile(
    r"(?P<head>[^ \[\]]*) (?P<base>[^ \[\]]+)\[(?P<reading>[^\]]*)\](?P<tail>[^ \[\]]*)"
)


@dataclass
class Reading:
    """A kanji word of the sentence and the reading written for it."""

    line: int
    start: int  # in the line without the captions' readings
    end: int
    surface: str
    draft: str  # Sudachi's reading
    # What the audio settled: None leaves the word for review (`doubt_choices`)
    written: Optional[str]
    # "caption", "draft", "correction"; for a review "review" (the transcripts did not settle
    # it), "mismatch" (a transcript is far from the caption) or "unplaced" (the word starts
    # inside a morpheme of the line as read again, so no stretch of the audio is its)
    why: str
    # Each transcript's stretch at the word, katakana morae as said (`reading_check.morae`);
    # none for an unplaced word
    heard: list[str] = field(default_factory=list)
    # For a word the transcripts did not settle, the other readings they heard there: one of
    # its choices a transcript hears, and their stretches where all of them heard it alike. A
    # stretch is the audio between the word's aligned neighbours, so where they heard it two
    # ways its edges hold a neighbour's morae (小鈴 heard ウシャオリン and オシャオリ), and
    # offered whole such stretches made the furigana a jumble
    offered: list[str] = field(default_factory=list)
    # The readings the word may be written with besides its draft (`licensed_readings`)
    known: list[str] = field(default_factory=list)


@dataclass
class Furigana:
    texts: list[str]  # each spoken line without the captions' readings
    readings: list[Reading]
    mismatched: bool  # a transcript is far from the caption

    @property
    def lines(self) -> list[str]:
        """Each spoken line with its furigana, Anki's 漢字[かな], as a field's HTML: the plain
        text's &, < and > escaped."""
        return [
            escape(furigana_line(text, [r for r in self.readings if r.line == n]), quote=False)
            for n, text in enumerate(self.texts)
        ]

    @property
    def text(self) -> str:
        return "<br>".join(self.lines)

    @property
    def review(self) -> list[Reading]:
        return [r for r in self.readings if r.written is None]


def licensed_readings(
    word: Word, dictionary: Dictionary, names: Mapping[str, str]
) -> list[str]:
    """What may be written for a word besides its draft: the dictionary's readings of its
    spelling, a number's in kanji numerals too (10日 as 十日), and for a name the show's."""
    forms = {word.surface, kanji_numerals(word.surface)}
    found: set[str] = set()
    for form in forms:
        found.update(dictionary(form))
    out = sorted(found)
    if "name" in word.kinds and word.surface in names:
        out.append(names[word.surface])
    return out


def audio_furigana(
    caption: str,
    transcripts: Sequence[str],
    tokenize: Callable[[str], Sequence],
    names: Mapping[str, str],
    dictionary: Dictionary,
    *,
    base: BaseFinder = whole_run,
    show_words: Container[str] = (),
    quorum: int = 1,
) -> Furigana:
    """The caption's furigana as the transcripts hear it. `names` is the show's names, surface
    to reading; `show_words` the surfaces its captions read in hiragana somewhere, which are no
    names whatever Sudachi's dictionary says (薬師 is the apothecary くすし there); `base`
    places the captions' own readings (captions.ReadingBase); a reading is written where
    `quorum` of the transcripts hear it (reading_check.decide)."""
    texts: list[str] = []
    words: list[Word] = []
    for n, line in enumerate(spoken_lines(caption)):
        text, inline = split_readings(line, base)
        texts.append(text)
        morphemes = list(tokenize(text))
        line_words = kanji_words(
            n, text, morphemes, inline, names, lambda s: numeral_reading(s, tokenize)
        )
        for w in line_words:
            if w.surface in show_words and "name" in w.kinds and w.caption is None:
                w.kinds.remove("name")
        words.extend(line_words)
    spans = [Span(w.line, w.start, w.end, w.caption or w.sudachi) for w in words]
    lines = reference(texts, spans, tokenize)
    licensed = [licensed_readings(w, dictionary, names) for w in words]
    options = [choices(w.sudachi, licensed[i]) for i, w in enumerate(words)]
    # The words a transcript far from the caption is lined up again by, read as it hears them
    as_heard = {i: options[i] for i, w in enumerate(words) if w.caption is None}
    hearings: dict[int, list[dict[str, bool]]] = {i: [] for i in lines.placed}
    stretches: dict[int, list[str]] = {i: [] for i in lines.placed}
    mismatched = not transcripts
    for transcript in transcripts:
        hyp = morae(transcript)
        heard_on, pairs, far = line_up(texts, spans, lines, hyp, as_heard, tokenize)
        mismatched |= far
        for k, i in enumerate(heard_on.placed):
            lo, hi = neighbours(heard_on.owner, pairs, k, len(hyp), heard_on.starts[k])
            hearings[i].append(hears_choices(options[i], hyp, lo, hi))
            stretches[i].append("".join(hyp[lo:hi]))
    readings: list[Reading] = []
    for i, w in enumerate(words):
        written: Optional[str]
        if w.caption is not None:
            written, why = w.caption, "caption"
        elif mismatched:
            written, why = None, "mismatch"
        elif i not in hearings:
            written, why = None, "unplaced"
        else:
            written = decide(hearings[i], w.sudachi, quorum, licensed[i])
            why = "review" if written is None else "draft" if written == w.sudachi else "correction"
            if written is not None:
                written = spelt_as(written, licensed[i])
        if written is not None and not writable(w.surface, written):
            # Latin letters a reading does not read by their names (Ｗ杯, わーるどかっぷ): no
            # furigana leaves them out of its brackets, so a person writes the word
            written, why = None, "review"
        heard = stretches.get(i, [])
        reading = Reading(w.line, w.start, w.end, w.surface, w.sudachi, written, why, heard)
        reading.known = licensed[i]
        if why == "review" and i in hearings:
            reading.offered = offered_readings(options[i], hearings[i], heard)
        readings.append(reading)
    return Furigana(texts, readings, mismatched)


def offered_readings(
    options: Mapping[str, str], hearings: Sequence[Mapping[str, bool]], heard: Sequence[str]
) -> list[str]:
    """What else the transcripts heard at a word they did not settle (`Reading.offered`)."""
    out = [r for key, r in options.items() if key != "draft" and any(h.get(key) for h in hearings)]
    kana = [to_hiragana(stretch) for stretch in heard]
    if kana and all(kana) and all(agrees(kana[0], k) for k in kana[1:]):
        out.append(kana[0])
    return out


def furigana_line(text: str, readings: Sequence[Reading]) -> str:
    """A line with each word's furigana: the reading written, none where that holds a kanji
    Sudachi could not read, and for a word in review the readings to choose between
    (`doubt_furigana`)."""
    out: list[str] = []
    pos = 0
    for r in sorted(readings, key=lambda r: r.start):
        out.append(text[pos : r.start])
        if r.written is None:
            out.append(doubt_furigana(r.surface, doubt_choices(r)))
        elif KANJI_RE.search(r.written):
            out.append(r.surface)
        else:
            out.append(word_furigana(r.surface, r.written))
        pos = r.end
    out.append(text[pos:])
    # Anki's furigana takes a space before a word as its start; at the line's start none is
    # needed, and the notes have none there
    return "".join(out).lstrip(" ")


def word_furigana(surface: str, reading: str) -> str:
    """A word's furigana, Anki's 漢字[かな], as make_furigana_from_reading writes it, which
    leaves the word's kana out of the brackets: but for katakana at its end, which it takes in
    with the rest (十センチ[じゅっせんち]), so they are left out here: 十[じゅっ]センチ. Latin
    letters at either end are left out too, their names taken off the reading
    (`captions.read_letters`): Ｂ型 read びーがた is Ｂ 型[がた], a letter being its own
    reading. Where the reading does not read them so, they stay in (`writable`)."""
    split = split_letters(surface, reading)
    if split is not None and (split[0] or split[3]):
        before, rest, rest_reading, after = split
        return before + word_furigana(rest, rest_reading) + after
    tail = KATAKANA_TAIL_RE.search(surface)
    if tail and tail.start() and to_hiragana(reading).endswith(to_hiragana(tail.group())):
        head = reading[: len(reading) - len(tail.group())]
        return make_furigana_from_reading(surface[: tail.start()], head) + tail.group()
    return make_furigana_from_reading(surface, reading)


def split_letters(surface: str, reading: str) -> Optional[tuple[str, str, str, str]]:
    """The Latin letters at the start of a word, the rest and its reading, and the letters at
    its end, the letters read by their names (`captions.read_letters`): Ｂ型 read びーがた is
    Ｂ, 型, がた and none. None where the reading does not read them so (Ｗ杯, わーるどかっぷ),
    where one is inside the word, or where they are all of it."""
    match = LETTERS_AROUND_RE.fullmatch(surface)
    before, rest, after = match.groups() if match else ("", surface, "")
    if not before and not after:
        return "", surface, reading, ""
    if not rest or LATIN_RE.search(rest):
        return None
    start = read_letters(before, reading) if before else 0
    end = read_letters(after, reading[start:], at_end=True) if after and start is not None else 0
    if start is None or end is None or start + end >= len(reading):
        return None
    return before, rest, reading[start : len(reading) - end], after


def writable(surface: str, reading: str) -> bool:
    """Whether `reading` can be written over `surface` with its Latin letters out of the
    brackets (`word_furigana`), each read by its name: kana-highlight makes junk of a letter
    given a reading, and a letter is its own reading anyway."""
    return split_letters(surface, reading) is not None


def doubt_choices(r: Reading) -> list[str]:
    """The readings a word in review is written with: its draft, unless that holds a kanji
    Sudachi could not read, then each reading it is `offered` that sounds otherwise than the
    ones before. A word left for a mismatch is offered none: what the alignment puts at a word
    of a line its clip does not match may be another line's speech."""
    out = [] if KANJI_RE.search(r.draft) else [r.draft]
    for kana in r.offered:
        if not any(agrees(kana, o) for o in out):
            out.append(kana)
    return out


def doubt_furigana(surface: str, choices: Sequence[str]) -> str:
    """A word in review, its `choices` joined by DOUBT and a lone one followed by it: split
    as its draft is where the word is one group and every choice leaves the same kana outside
    it (開[ひら？あ]か), else as one group over the whole word (泣き明かし[なきあかし？なきかし]),
    which a reading that splits otherwise needs."""

    def joined(readings: Sequence[str]) -> str:
        return DOUBT.join(readings) + (DOUBT if len(readings) < 2 else "")

    groups = [ONE_GROUP_RE.fullmatch(word_furigana(surface, c)) for c in choices]
    shapes = {(m["head"], m["base"], m["tail"]) for m in groups if m}
    if choices and all(groups) and len(shapes) == 1:
        head, base, tail = shapes.pop()
        return f"{head} {base}[{joined([m['reading'] for m in groups if m])}]{tail}"
    return f" {surface}[{joined(choices)}]"


def has_doubt(text: str) -> bool:
    """Whether a furigana text still holds a word in review, written as `doubt_furigana`
    writes one."""
    return bool(DOUBT_RE.search(text))
