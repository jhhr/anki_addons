"""The words "Set furigana from audio" leaves for review, settled by a language model where the
audio allows: the arbiter.

The audio rule (`audio_furigana.py`) writes a reading only where the transcripts hear the draft,
or another reading the dictionary has for the spelling or the show has for a name. In the
furigana audio eval that left two kinds of word for a person. Most were heard alike by both
transcripts as a reading nothing licenses, and that was the reading said, the draft wrong: a
sound change (話 ばなし, 小屋 ごや), a conjugated form JMdict has no entry for (開か あか), a
reading of the show's world (女官 にょかん, 公主 ひめ), a name no list has. The rest the
transcripts heard two ways (方 かた and ほう), or both misheard alike (下女 heard ケジョ). A
model reading the line, its English subtitle, the transcripts and what each heard at the word
tells most of the first kind from the last, and some of the second.

It cannot hear the clip, and a model would sooner give a word its usual reading than the one
said, so it only chooses among what was heard: a reading it gives is written where it says it
is sure, a transcript heard that reading at the word (`heard_at`), and the reading keeps the
word's own kana (`keeps_kana`: 困っ is never こま). Anything else stays in review, written with
the readings to choose between as before.

One model alone is wrong too often for furigana meant to be right: of the eval's labelled words
it settled, Claude Haiku 5.5 got 1 of 73 wrong (好き as ずき, where the transcripts split),
Sonnet 5.5 3 of 74 (妓女 as げじょ twice, which both transcripts misheard, and 診療所 as
しんりょうしょ) and Opus 5.5 1 of 80 (the same 好き). So the op asks every model of a list
(`settle`), and a reading is written only where all of them give it: Haiku and Sonnet together
got none of their 66 wrong, settling 165 of the 360 words in review, as did Sonnet and Opus. A
mistake two models share gets through: Haiku and Opus kept their 好き.

One call a note and model, for all its words in review that a transcript heard something at
(`asked`): the line's other words are context for each. A word no transcript heard anything
at can never be written (`heard_at`), so it is not asked about, and a note with only such
words makes no call. The prompt is built from `review_inputs`' values alone, which the call
records as its inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ..html_stripping import text_lines
from ..kana_conv import to_hiragana
from .audio_furigana import Furigana, Reading
from .captions import KANJI_RE, kanji_numerals, spoken_lines
from .reading_check import AGREE, fit, morae, same_reading

WORDS = "words"
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        WORDS: {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "why": {"type": "string"},
                    "reading": {"type": "string"},
                    "sure": {"type": "boolean"},
                },
                "required": ["n", "why", "reading", "sure"],
                "additionalProperties": False,
            },
        }
    },
    "required": [WORDS],
    "additionalProperties": False,
}
KANA_RE = re.compile(r"[ぁ-ゖァ-ヺー]+")

PROMPT_HEAD = """You decide how a speaker read some words of a line of Japanese anime dialogue, for the furigana of a flash card.

## What you are given

The caption: the line as the show's Japanese closed captions write it, one subtitle line a row. A reading the captions give themselves follows its kanji in parentheses: 雪華(セッカ).

The English subtitle of the same lines, if there is one. Its romanized names (Sekka) tell how a name is read.

The transcripts: what speech recognition models heard in the clip, in kana as it sounds (the particle は as わ, a long vowel as a vowel or ー), without having seen the caption. They mishear, mostly names, rare words and unclear speech, and they can disagree with each other. The clip may lack part of the caption, or hold speech the caption does not.

The words: each kanji word of the caption whose reading the audio did not settle, numbered, with
- the word in its line, marked ［］;
- the reading a tokenizer gave it, which is often wrong for names and for words read in an unusual way;
- the readings known for it: a dictionary's for its spelling, and the show's for a name;
- what each transcript heard at that place, as well as it could be lined up with the caption: it may take in a sound of the word before or after, or miss one.

## What to answer

For each word, the reading the speaker said, in hiragana: of exactly the text in ［］ with any kana in it (行か said いか is いか), and with the sound changes it has in this line (箱 in 宝箱 is ばこ).

Say you are sure only when both hold:
- a transcript heard this reading at the word, give or take a mishearing as small as a long vowel or a っ;
- the word can have this reading here: a known reading, a sound change or conjugated form of one, a reading of the show's world (an old or Chinese-style reading of a title or a name), or for a name a reading both transcripts or the subtitle back.

Otherwise say you are not sure, and give your best guess. Where the transcripts heard the word two ways, choose by the context only when it leaves no doubt (市場 of a town's market is いちば); where both readings fit the line (最中 さいちゅう or さなか), you are not sure. Where both transcripts heard something that is no reading of the word (地図 heard ちつ), they misheard it: you are not sure. A reading no transcript heard at the word is never written, so answer from the audio, not from how the word is usually read.

"""

PROMPT_TAIL = """
## Output

A JSON object: {"words": [{"n": 1, "why": "...", "reading": "...", "sure": true}, ...]}, one item a word in the order given. "why" is a few words on the evidence, "reading" the reading in hiragana, "sure" true or false.
"""


@dataclass
class Answer:
    reading: str  # hiragana
    sure: bool


def translation_text(field: str) -> str:
    """A translation field as the prompt shows it: its lines a row each, no other HTML. A line
    is one of a <br> or of a <div> block, which Anki's editor writes (`text_lines`); a field of
    <br> lines only comes out as it always did, so the eval's cached answers stay its own."""
    return "\n".join(text_lines(field)).strip()


def heard_something(r: Reading) -> bool:
    """Whether a transcript heard a mora at the word: `heard_at` needs one, so a word every
    transcript passed over (a clip that stops early) can never be written."""
    return any(morae(stretch) for stretch in r.heard)


def asked(furigana: Furigana) -> list[Reading]:
    """The words in review the models are asked about, in the order `review_inputs` and
    `settle` number them: those a transcript heard something at."""
    return [r for r in furigana.review if heard_something(r)]


def review_inputs(
    caption: str, translation: str, transcripts: Sequence[tuple[str, str]], furigana: Furigana
) -> dict:
    """What the prompt for `furigana`'s words in review is built from: the caption's spoken
    lines, the translation, each transcript by its transcriber's name, and for each word
    asked about (`asked`) its line with the word marked, its draft, its known readings and
    what each transcript heard there."""
    words = []
    for r in asked(furigana):
        text = furigana.texts[r.line]
        words.append(
            {
                "context": f"{text[: r.start]}［{r.surface}］{text[r.end :]}",
                "draft": r.draft,
                "known": list(r.known),
                "heard": [to_hiragana(stretch) for stretch in r.heard],
            }
        )
    return {
        "lines": spoken_lines(caption),
        "translation": translation,
        "transcripts": [{"name": name, "text": text} for name, text in transcripts],
        "words": words,
    }


def review_prompt(
    lines: Sequence[str],
    translation: str,
    transcripts: Sequence[Mapping[str, str]],
    words: Sequence[Mapping[str, Any]],
) -> str:
    names = [t["name"] for t in transcripts]
    parts = [PROMPT_HEAD, "## Caption\n\n", "\n".join(lines), "\n\n"]
    if translation.strip():
        parts += ["## English subtitle\n\n", translation.strip(), "\n\n"]
    parts.append("## Transcripts\n\n")
    parts += [f"{t['name']}: {t['text']}\n" for t in transcripts]
    parts.append("\n## Words\n\n")
    for n, word in enumerate(words, 1):
        heard = ", ".join(
            f"{name} {kana or '(nothing)'}" for name, kana in zip(names, word["heard"])
        )
        parts += [
            f"{n}. {word['context']}\n",
            f"   tokenizer's reading: {word['draft']}\n",
            f"   known readings: {', '.join(word['known']) or '(none)'}\n",
            f"   heard: {heard or '(not lined up)'}\n",
        ]
    parts.append(PROMPT_TAIL)
    return "".join(parts)


def answers_from_response(response: Any, count: int) -> dict[int, Answer]:
    """The answer for each word by its number, 1 to `count`. A word answered twice, or with a
    reading that is not all kana, has none, and neither does a number that is a boolean,
    which Python takes for 1 or 0: a provider that does not enforce the schema can send one."""
    items = response.get(WORDS) if isinstance(response, dict) else None
    if not isinstance(items, list):
        return {}
    found: dict[int, list[Answer]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        n, reading, sure = item.get("n"), item.get("reading"), item.get("sure")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= count:
            continue
        if not isinstance(reading, str):
            continue
        reading = reading.strip()
        if KANA_RE.fullmatch(reading):
            found.setdefault(n, []).append(Answer(to_hiragana(reading), sure is True))
    return {n: got[0] for n, got in found.items() if len(got) == 1}


def heard_at(reading: str, heard: Sequence[str]) -> bool:
    """Whether a transcript heard `reading` at its word: it agrees with the word's stretch of
    one of them, a mora at either edge of the stretch spared, since the stretch between a
    word's aligned neighbours can take in a mora of theirs (流行 heard ハヤリヤ)."""
    for stretch in heard:
        hyp = morae(stretch)
        if hyp and fit(reading, hyp, 0, len(hyp)) <= AGREE + 1e-9:
            return True
    return False


def keeps_kana(surface: str, reading: str) -> bool:
    """Whether `reading` holds the kana `surface` is written with, where they are: an answer
    for 困っ that drops its っ still fits a transcript's コマッ a mora short. A number in
    digits is matched as spelt in kanji numerals (`captions.kanji_numerals`, 10日 as 十日),
    as the rule reads it, and its 〇 is read like any numeral kanji: a digit is no kana a
    reading could hold, and 7時 said しちじ was otherwise never settled."""
    pattern = re.sub(
        r"(?:\.\+)+",
        ".+",
        "".join(
            ".+" if KANJI_RE.match(c) or c == "〇" else re.escape(to_hiragana(c))
            for c in kanji_numerals(surface)
        ),
    )
    return re.fullmatch(pattern, to_hiragana(reading)) is not None


def agreed_reading(r: Reading, answers: Sequence[Optional[Answer]]) -> Optional[str]:
    """The reading to write for a word in review given each model's answer for it: the one
    every model is sure of, heard at the word and keeping its kana; None leaves it in review."""
    readings = [
        a.reading
        for a in answers
        if a is not None
        and a.sure
        and keeps_kana(r.surface, a.reading)
        and heard_at(a.reading, r.heard)
    ]
    if not answers or len(readings) < len(answers):
        return None
    if any(not same_reading(readings[0], other) for other in readings[1:]):
        return None
    return readings[0]


def settle(furigana: Furigana, answers: Sequence[Mapping[int, Answer]]) -> list[Reading]:
    """Writes the words asked about, numbered as `review_inputs` numbers them (`asked`), that
    every model's answers agree on (`agreed_reading`); the words it wrote."""
    settled = []
    for n, r in enumerate(asked(furigana), 1):
        written = agreed_reading(r, [by_model.get(n) for by_model in answers])
        if written is not None:
            r.written, r.why = written, "arbiter"
            settled.append(r)
    return settled
