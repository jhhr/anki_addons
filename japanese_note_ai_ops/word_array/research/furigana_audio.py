"""What the furigana audio eval's scripts share: where its files are, the rows of a subs2srs
export and how a subtitle line is cleaned before its readings are drafted.

The eval measures how well a sentence's furigana can be set from its audio clip: which
transcriber or reading check gets the reading the speaker used, for the names and the words
with several readings that a tokenizer gets wrong. Its corpus is a subs2srs export of one show,
a TSV with a card per row and the card's audio clip named in it. Everything the scripts write
holds that show's subtitles, so it lives in the private test data checkout
(`evals/furigana_audio/`, `_bootstrap.eval_file`), never in this repo.

    furigana_audio_select.py   export -> the lines to label, and why each was picked

The subtitles are Japanese closed captions, which differ from the dialogue in ways the
cleaning has to know about:
- a speaker label opens a speaker's part, `（猫猫）ん？`, and a sound description stands alone,
  `（赤ん坊のはしゃぎ声）`. Neither is spoken, so both are dropped, full-width parentheses and
  all;
- a reading follows a word in half-width parentheses, `猫猫(マオマオ)` or `緑青館(ろくしょうかん)`,
  usually only at the word's first mention in an episode, sometimes inside a label
  (`（羅門(ルォメン)）`). It is the caption writer's reading, so it is kept as the word's furigana.
  The word can follow other kanji, `白鈴姐(ねえ)` or `鈴麗公主(ひめ)`, so which of them the
  reading is for is a guess (`split_readings`' `base`);
- ASS override tags (`{\\an8}`) mark a line drawn at the top of the screen while another one
  runs at the bottom. subs2srs merged such overlapping events into one card and repeated
  their text, so the card's text and its audio no longer match.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from _bootstrap import eval_file

HOME = eval_file("furigana_audio")
EXPORT = HOME / "kusuriya_ep1-49_sentences.tsv"
SELECTION = HOME / "selection.jsonl"
CAPTION_READINGS = HOME / "caption_readings.jsonl"
AUDIO = HOME / "audio"

KANJI = "一-鿿㐀-䶿々〆ヶ"
KANJI_RE = re.compile(f"[{KANJI}]")
# A reading the captions give in half-width parentheses right after the kanji it reads
INLINE_READING_RE = re.compile(f"([{KANJI}]+)\\(([ぁ-ゖァ-ヺー・]+)\\)")
# Labels and sound descriptions; the half-width parentheses of a reading inside a label are no
# delimiter here, so （羅門(ルォメン)） is one segment
UNSPOKEN_RE = re.compile(r"（[^（）]*）")
ASS_TAG_RE = re.compile(r"\{\\[^}]*\}")
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
# A media column of a subs2srs export: its audio, snapshot, animated snapshot and video
MEDIA_RE = re.compile(r"^(\[sound:|<img |\[video:)")
KATAKANA_RE = re.compile(r"[ァ-ヺー・]+")


class Row(NamedTuple):
    id: str  # the sequence marker: episode, card number and start time, unique in an export
    episode: str
    audio: str  # the clip's file name, as the [sound:] tag names it
    jp: str  # subs1 as exported
    en: str  # subs2 as exported


def read_export(path: Path = EXPORT) -> tuple[list[Row], list[str]]:
    """The export's rows, and why each line that is no card was left out.

    subs2srs writes the tag and the sequence marker first, then a column per media type it
    made (audio, snapshot, animated snapshot, video), then subs1 and subs2, then any context
    lines. So subs1 and subs2 are the two columns after the last media one, however many media
    types the export has. A line with no audio, or no subs1 or subs2 there, is no card: the
    export has one whose only text is `6`."""
    rows: list[Row] = []
    skipped: list[str] = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            cols = line.rstrip("\r\n").split("\t")
            sound = next((m for m in map(SOUND_RE.search, cols) if m), None)
            media = [i for i, col in enumerate(cols) if MEDIA_RE.match(col)]
            texts = cols[media[-1] + 1 : media[-1] + 3] if media else []
            complete = len(texts) == 2 and all(t.strip() for t in texts)
            if len(cols) < 2 or sound is None or not complete:
                skipped.append(f"line {n}: {' | '.join(cols[1:2] + texts)[:80]}")
                continue
            seq = cols[1]
            rows.append(Row(seq, seq.split("_", 1)[0], sound.group(1), texts[0], texts[1]))
    return rows, skipped


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def is_noisy(jp: str) -> bool:
    """A card whose text holds overlapping caption events, merged and repeated: its audio
    says each once, so the text is no transcript of it."""
    return ASS_TAG_RE.search(jp) is not None


def spoken_lines(jp: str) -> list[str]:
    """The card's caption lines with what is not spoken taken out: ASS tags, speaker labels and
    sound descriptions. Inline readings stay, `猫猫(マオマオ)`; a line left empty is dropped."""
    out = []
    for line in ASS_TAG_RE.sub("", jp).split("<br>"):
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
    for line in ASS_TAG_RE.sub("", jp).split("<br>"):
        found.extend(split_readings(line, base)[1])
    return found


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n

