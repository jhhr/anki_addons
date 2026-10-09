"""Score the furigana audio eval's runs: how far each transcriber's kana give the readings of
the picked cards' kanji words.

There are no hand labels yet, so each word is scored against what is known of its reading:

- `caption`: the line reads it itself (`妃(きさき)`). The words where Sudachi reads it
  otherwise are the test that matters: how often a model's kana give the caption's reading,
  and how often Sudachi's instead;
- `show name`: a name the captions read in katakana in another line of the show (猫猫 is
  まおまお in every episode), here without a reading of its own;
- `show word`: likewise a word the captions read in hiragana elsewhere. Softer, since such a
  word can be read otherwise in another context (妃 is きさき alone, ひ after a name);
- `draft`: Sudachi's reading, per kind of word. On ordinary words it is nearly always right,
  so a model disagreeing with it there is mostly the model's error, and a review a note would
  get for nothing.

A transcript is compared with the line as pronounced. Both become katakana morae: a long
vowel mark is the vowel it lengthens, は/へ/を as particles are ワ/エ/オ, ヂ/ヅ are ジ/ズ. Then
they are aligned by edit distance, with a cheap step for the spellings of one sound (オウ and
オー, エイ and エー, a dropped っ or long vowel), and each word gets the stretch of the
transcript that lines up with its reading. A word agrees with a reading when only cheap steps
separate them.

Writes `runs/<model>.words.jsonl` (each word with what the model heard, for the labelling) and
prints a table per model.

    python word_array/research/furigana_audio_score.py [MODEL ...]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import furigana_audio as fa
from furigana_audio_run import RUNS
from furigana_audio_select import sudachi_tokenizer

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
RUBY_RE = re.compile(r"[^\s\[\]]+?\[([^\]]*)\]")
KINDS = ("name", "caption_word", "ambiguous", "number")
# Two readings agree when the steps between them cost no more than this: cheap ones only
AGREE = 0.45
CHEAP = 0.2


def to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


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
    return align(morae(a), morae(b))[0] <= AGREE


def heard_text(model: str, text: str) -> str:
    """The kana a model's answer gives: Ruby-ASR's ruby text keeps its readings and loses the
    kanji they read; the kana models' answers are kana already, marks aside."""
    if model == "ruby":
        text = RUBY_RE.sub(lambda m: m.group(1), text)
    return text


@dataclass
class WordResult:
    card: str
    word: dict
    expected: str  # the reading the line is aligned with: the caption's, else Sudachi's
    heard: str = ""
    verdicts: dict = field(default_factory=dict)

    def row(self) -> dict:
        w = self.word
        return {
            "id": self.card,
            "line": w["line"],
            "surface": w["surface"],
            "sudachi": w["sudachi"],
            "caption": w["caption"],
            "kinds": w["kinds"],
            "heard": self.heard,
            **self.verdicts,
        }


def reference(
    card: dict, tokenize: Callable[[str], Sequence]
) -> tuple[list[str], list[int], list[WordResult]]:
    """The card's spoken lines as morae, which word each mora belongs to (-1 for none), and
    the words. A kanji word is read as the caption reads it, else as Sudachi does; the rest of
    the line as Sudachi reads it, particles as pronounced."""
    ref: list[str] = []
    owner: list[int] = []
    words: list[WordResult] = []
    by_line: dict[int, list[dict]] = defaultdict(list)
    for w in card["words"]:
        by_line[w["line"]].append(w)
    for n, line in enumerate(card["lines"]):
        text, _ = fa.split_readings(line)
        spans = sorted(by_line.get(n, []), key=lambda w: w["start"])
        k = 0
        for mo in tokenize(text):
            while k < len(spans) and spans[k]["end"] <= mo.begin():
                k += 1
            if k < len(spans) and spans[k]["start"] <= mo.begin() < spans[k]["end"]:
                if mo.begin() == spans[k]["start"]:
                    w = spans[k]
                    reading = w["caption"] or w["sudachi"]
                    words.append(WordResult(card["id"], w, reading))
                    got = morae(reading)
                    ref.extend(got)
                    owner.extend([len(words) - 1] * len(got))
                continue
            surface = mo.surface()
            if mo.part_of_speech()[0] == "助詞" and to_katakana(surface) in PARTICLE_SOUND:
                got = [PARTICLE_SOUND[to_katakana(surface)]]
            else:
                got = morae(mo.reading_form())
            ref.extend(got)
            owner.extend([-1] * len(got))
    return ref, owner, words


def score_card(
    card: dict, text: str, model: str, tokenize: Callable[[str], Sequence], show: dict
) -> tuple[list[WordResult], float, int]:
    """The card's words with what the model heard for each, the alignment's cost and the
    number of reference morae."""
    ref, owner, words = reference(card, tokenize)
    hyp = morae(heard_text(model, text))
    cost, pairs = align(ref, hyp)
    spans: dict[int, list[int]] = defaultdict(list)
    for i, j in enumerate(pairs):
        if owner[i] >= 0 and j is not None:
            spans[owner[i]].append(j)
    for k, result in enumerate(words):
        js = spans.get(k)
        result.heard = "".join(hyp[min(js) : max(js) + 1]) if js else ""
        w = result.word
        result.verdicts["draft"] = agrees(result.heard, w["sudachi"])
        if w["caption"]:
            result.verdicts["caption"] = agrees(result.heard, w["caption"])
        elif w["surface"] in show:
            script, reading = show[w["surface"]]
            result.verdicts[f"show_{script}"] = agrees(result.heard, reading)
    return words, cost, len(ref)


def show_readings() -> dict[str, tuple[str, str]]:
    """surface -> (script, its most common caption reading) over the show."""
    out = {}
    for row in fa.read_jsonl(fa.CAPTION_READINGS):
        reading = max(row["readings"], key=row["readings"].get)
        out[row["surface"]] = ("name" if row["script"] == "katakana" else "word", reading)
    return out


def rate(results: Sequence[WordResult], key: str, test: Callable[[WordResult], bool]) -> str:
    chosen = [r for r in results if test(r) and key in r.verdicts]
    if not chosen:
        return "-"
    hits = sum(r.verdicts[key] for r in chosen)
    return f"{100 * hits / len(chosen):5.1f}% of {len(chosen)}"


def report(
    model: str, results: Sequence[WordResult], cost: float, ref_len: int, secs: Sequence[float]
) -> str:
    def caption_differs(r: WordResult) -> bool:
        return bool(r.word["caption"]) and not agrees(r.word["caption"], r.word["sudachi"])

    def caption_same(r: WordResult) -> bool:
        return bool(r.word["caption"]) and agrees(r.word["caption"], r.word["sudachi"])

    def has(key: str) -> Callable[[WordResult], bool]:
        return lambda r: key in r.verdicts

    def kind(name: str) -> Callable[[WordResult], bool]:
        return lambda r: name in r.word["kinds"]

    rows = [
        ("caption reading Sudachi gets wrong", "caption", caption_differs, "the caption's"),
        ("", "draft", caption_differs, "Sudachi's"),
        ("caption reading Sudachi gets right", "caption", caption_same, "it"),
        ("show name, no reading in the line", "show_name", has("show_name"), "the show's"),
        ("", "draft", has("show_name"), "Sudachi's"),
        ("show word, no reading in the line", "show_word", has("show_word"), "the show's"),
        ("", "draft", has("show_word"), "Sudachi's"),
    ]
    rows += [(f"{name} word", "draft", kind(name), "Sudachi's") for name in KINDS]
    rows.append(("ordinary word", "draft", lambda r: not r.word["kinds"], "Sudachi's"))
    mean = sum(secs) / max(len(secs), 1)
    lines = [
        f"== {model}: {len(secs)} cards, {mean:.1f} s a card,"
        f" morae off {100 * cost / max(ref_len, 1):.1f}% of the line as read"
    ]
    for label, key, test, whose in rows:
        lines.append(f"  {label:<36} hears {whose:<13} {rate(results, key, test)}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("models", nargs="*", help="the runs to score (default: every run there is)")
    args = ap.parse_args(argv)
    models = args.models or sorted(
        p.stem for p in RUNS.glob("*.jsonl") if not p.stem.endswith(".words")
    )
    cards = {c["id"]: c for c in fa.read_jsonl(fa.SELECTION)}
    tokenize = sudachi_tokenizer()
    show = show_readings()
    for model in models:
        rows = fa.read_jsonl(RUNS / f"{model}.jsonl")
        results: list[WordResult] = []
        total_cost, total_ref = 0.0, 0
        for row in rows:
            if row["id"] not in cards:
                continue
            words, cost, ref_len = score_card(cards[row["id"]], row["text"], model, tokenize, show)
            results.extend(words)
            total_cost += cost
            total_ref += ref_len
        fa.write_jsonl(RUNS / f"{model}.words.jsonl", (r.row() for r in results))
        print(report(model, results, total_cost, total_ref, [r["seconds"] for r in rows]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
