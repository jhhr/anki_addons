"""What the furigana audio eval's scripts share: where its files are, the rows of a subs2srs
export, and the labelling's files.

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

The subtitles are Japanese closed captions, cleaned as the op cleans a sentence note's
(`word_array/captions.py`: speaker labels and sound descriptions dropped, the captions' own
readings kept), so the eval reads each line as the op will.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, NamedTuple, Optional

from _bootstrap import eval_file, load

if TYPE_CHECKING:
    # `load()` gives a module object, so what comes out of it is untyped; the real module is
    # named for type checking only (the runtime import is what _bootstrap avoids).
    from japanese_note_ai_ops.word_array import captions
else:
    captions = load("captions")

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

# The scripts here reach the op's caption cleaning as fa.<name>
KANJI = captions.KANJI
KANJI_RE = captions.KANJI_RE
BaseFinder = captions.BaseFinder
whole_run = captions.whole_run
kanji_numerals = captions.kanji_numerals
to_hiragana = captions.to_hiragana
spoken_lines = captions.spoken_lines
split_readings = captions.split_readings
all_inline_readings = captions.all_inline_readings


def is_noisy(jp: str) -> bool:
    """Whether a card holds an ASS override tag. The op drops a tag as formatting, but in this
    show the cards subs2srs merged from overlapping caption events, their text repeated, carry
    one, so the selection leaves every tagged card out (41 of its 53 are said once as written)."""
    return captions.ASS_TAG_RE.search(jp) is not None

SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
# A media column of a subs2srs export: its audio, snapshot, animated snapshot and video
MEDIA_RE = re.compile(r"^(\[sound:|<img |\[video:)")
# A reading typed after its kanji as Anki writes furigana, 父上[ちちうえ], or in full-width
# parentheses, 父上（ちちうえ）
TYPED_READING_RE = re.compile(f"([{KANJI}]+)(?:\\[([ぁ-ゖァ-ヺー・]+)\\]|（([ぁ-ゖァ-ヺー・]+)）)")


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


def moved_labels(rows: Iterable[dict], old: str, new: str) -> list[dict]:
    """A line's labels, given on its text `old`, at the places their words have in `new`, the
    line corrected. A correction moves the words it leaves as they were (座っ of 養父上が座っ
    one place back in 父上が座っ), and a label kept at its old place would find another word
    there, or none. A label of a word the correction changed, or of none `old`
    has at its place (stale already), stays where it was, unless a moved one lands there."""
    blocks = difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks()
    stale, moved = [], []
    for row in rows:
        start, end = row["start"], row["start"] + len(row["surface"])
        shift = None
        if old[start:end] == row["surface"]:
            shift = next((b - a for a, b, size in blocks if a <= start and end <= a + size), None)
        if shift is None:
            stale.append(row)
        else:
            moved.append({**row, "start": start + shift, "end": end + shift})
    taken = {word_key(row) for row in moved}
    return [row for row in stale if word_key(row) not in taken] + moved


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

