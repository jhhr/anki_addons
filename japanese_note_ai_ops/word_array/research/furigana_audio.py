"""What the furigana audio eval's scripts share: where its files are, the rows of a subs2srs
export and how a subtitle line is cleaned before its readings are drafted.

The eval measures how well a sentence's furigana can be set from its audio clip: which
transcriber or reading check gets the reading the speaker used, for the names and the words
with several readings that a tokenizer gets wrong. Its corpus is a subs2srs export of one show,
a TSV with a card per row and the card's audio clip named in it. Everything the scripts write
holds that show's subtitles, so it lives in the private test data checkout
(`evals/furigana_audio/`, `_bootstrap.eval_file`), never in this repo.

    furigana_audio_select.py   export -> the lines to label, and why each was picked
    furigana_audio_copy.py     the picked cards' clips, from the subs2srs output
    furigana_audio_run.py      one transcriber over the clips
    furigana_audio_score.py    the runs against the captions' readings and the labels
    furigana_audio_label.py    the words worth an ear, and a page to label them by ear

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
from typing import Callable, Iterable, NamedTuple, Optional

from _bootstrap import eval_file

HOME = eval_file("furigana_audio")
EXPORT = HOME / "kusuriya_ep1-49_sentences.tsv"
SELECTION = HOME / "selection.jsonl"
CAPTION_READINGS = HOME / "caption_readings.jsonl"
AUDIO = HOME / "audio"
# The words to label by ear, a card a row, and the readings said, a word a row
LABEL_QUEUE = HOME / "label_queue.jsonl"
LABELS = HOME / "labels.jsonl"
# What the labelling found besides readings: the show's names with the reading said, and the
# caption lines that are not what the clip says, corrected
NAMES = HOME / "names.jsonl"
FIXES = HOME / "fixes.jsonl"

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
# A reading typed after its kanji as Anki writes furigana, 父上[ちちうえ], or in full-width
# parentheses, 父上（ちちうえ）
TYPED_READING_RE = re.compile(f"([{KANJI}]+)(?:\\[([ぁ-ゖァ-ヺー・]+)\\]|（([ぁ-ゖァ-ヺー・]+)）)")
DIGITS_RE = re.compile(r"[0-9０-９]+")
KANJI_DIGITS = "〇一二三四五六七八九"


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


# A word of a card: the card's id, its line and where it starts in the line without readings
WordKey = tuple[str, int, int]


def word_key(row: dict) -> WordKey:
    return (row["id"], row["line"], row["start"])


def label_for(labels: dict[WordKey, dict], key: WordKey, surface: str) -> Optional[dict]:
    """The label given to the word at `key`, if it was this word: one given before the words
    were found again to a word with another surface (七 where 七日 starts now) is stale."""
    found = labels.get(key)
    return found if found is not None and found.get("surface") == surface else None


def read_names(path: Optional[Path] = None) -> dict[str, str]:
    """The show's names given by ear: surface -> the reading said. A name is read so wherever it
    is written, so it is a known reading everywhere and asked about nowhere."""
    return {row["surface"]: row["reading"] for row in read_jsonl(path or NAMES)}


def read_fixes(path: Optional[Path] = None) -> dict[tuple[str, int], dict]:
    """The caption lines corrected by ear, by card id and line number: `text`, the line as
    the clip says it with any reading in the captions' half-width parentheses, empty where the
    clip does not hold the line at all, and `was`, the line before."""
    return {(row["id"], row["line"]): row for row in read_jsonl(path or FIXES)}


def typed_line(text: str) -> str:
    """A corrected line as typed, its readings written as the captions write them: 父上[ちちうえ]
    and 父上（ちちうえ） both become 父上(ちちうえ), which `split_readings` takes."""
    return TYPED_READING_RE.sub(lambda m: f"{m.group(1)}({m.group(2) or m.group(3)})", text.strip())


def read_labels(path: Optional[Path] = None) -> dict[WordKey, dict]:
    """The labels given by ear, by word: `reading` (kana) with `verdict` "heard", or none with
    "unsure" or "not_said"."""
    return {word_key(row): row for row in read_jsonl(path or LABELS)}


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n

