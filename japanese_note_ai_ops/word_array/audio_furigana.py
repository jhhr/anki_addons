"""Furigana for a sentence from its audio: the caption's kanji words read as the clip says.

A sentence mined from a show's captions (`captions.py`) is drafted by Sudachi word by word
(`kanji_words.py`), and each word's draft is checked against the kana transcripts of its clip
(`reading_check.py`). A word is written as the transcripts hear it: the draft, or another
reading the dictionary has for its spelling or the show's list has for a name. Anything else
goes to review, keeping the draft. A word the captions read themselves (猫猫(マオマオ)) is
written as they read it, which needs no audio. A transcript far from its caption (one the clip
cuts off, lines spoken over each other) is no evidence for any word of the sentence, so all of
them go to review, as do those of a caption of overlapping events (`captions.is_noisy`).

Speaker labels and sound descriptions are not spoken, so they are left out of the furigana
(captions.spoken_lines), and a word whose draft holds a kanji Sudachi could not read gets no
furigana until someone gives it one: none is better than a wrong one.

A transcript is read as kana: anything else in it is dropped, so a model writing ruby text
(漢字[かな]) gives its readings, and one writing ordinary text gives too little to check by.
Run over the furigana audio eval's cards with its two runs as the transcripts, this decides
every word as the eval's rule does (`research/furigana_audio_score.py --combine`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Container, Iterable, Mapping, Optional, Sequence

from ..shared.jp_text_processing.kana.make_furigana_from_reading import (
    make_furigana_from_reading,
)
from .captions import (
    KANJI_RE,
    BaseFinder,
    is_noisy,
    kanji_numerals,
    spoken_lines,
    split_readings,
    whole_run,
)
from .kanji_words import Word, kanji_words, numeral_reading
from .reading_check import (
    MISMATCH,
    Span,
    align,
    choices,
    decide,
    hears_choices,
    morae,
    neighbours,
    reference,
)

# A spelling's readings in the dictionary
Dictionary = Callable[[str], Iterable[str]]


@dataclass
class Reading:
    """A kanji word of the sentence and the reading written for it."""

    line: int
    start: int  # in the line without the captions' readings
    end: int
    surface: str
    draft: str  # Sudachi's reading
    # What the audio settled: None leaves the word for review, its draft written meanwhile
    written: Optional[str]
    # "caption", "draft", "correction"; for a review "review" (the transcripts did not settle
    # it), "mismatch" (a transcript is far from the caption) or "unplaced" (the word starts
    # inside a morpheme of the line as read again, so no stretch of the audio is its)
    why: str


@dataclass
class Furigana:
    lines: list[str]  # each spoken line with its furigana, Anki's 漢字[かな]
    readings: list[Reading]
    mismatched: bool  # a transcript is far from the caption

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
    ref, owner, placed = reference(texts, spans, tokenize)
    licensed = [licensed_readings(w, dictionary, names) for w in words]
    options = [choices(w.sudachi, licensed[i]) for i, w in enumerate(words)]
    hearings: dict[int, list[dict[str, bool]]] = {i: [] for i in placed}
    # A caption of overlapping events says each of them twice (captions.is_noisy): no clip
    # says that
    mismatched = not transcripts or is_noisy(caption)
    for transcript in transcripts:
        hyp = morae(transcript)
        cost, pairs = align(ref, hyp)
        mismatched |= cost > MISMATCH * max(len(ref), 1)
        for k, i in enumerate(placed):
            lo, hi = neighbours(owner, pairs, k, len(hyp))
            hearings[i].append(hears_choices(options[i], hyp, lo, hi))
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
        readings.append(Reading(w.line, w.start, w.end, w.surface, w.sudachi, written, why))
    lines = [
        furigana_line(text, [r for r in readings if r.line == n]) for n, text in enumerate(texts)
    ]
    return Furigana(lines, readings, mismatched)


def furigana_line(text: str, readings: Sequence[Reading]) -> str:
    """A line with each word's furigana: the reading written, else the draft, and none for a
    draft holding a kanji Sudachi could not read."""
    out: list[str] = []
    pos = 0
    for r in sorted(readings, key=lambda r: r.start):
        out.append(text[pos : r.start])
        reading = r.written if r.written is not None else r.draft
        if KANJI_RE.search(reading):
            out.append(r.surface)
        else:
            out.append(make_furigana_from_reading(r.surface, reading))
        pos = r.end
    out.append(text[pos:])
    # Anki's furigana takes a space before a word as its start; at the line's start none is
    # needed, and the notes have none there
    return "".join(out).lstrip(" ")
