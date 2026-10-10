"""Score the furigana audio eval's runs: how far each transcriber's kana give the readings of
the picked cards' kanji words.

Each word is scored against what is known of its reading: the reading `furigana_audio_label.py`
says was heard where a person labelled the word by ear, else

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

A transcript is compared with the line as pronounced, word by word, by the op's own check
(`word_array/reading_check.py`); one in ordinary text (anime-whisper's) is first read as
Sudachi reads it, so it tells a word's reading only where the model wrote the word in kana or
in other kanji than the caption's.

A card whose transcript is far from its line is mostly not the model's fault: the captions of
a card can hold text its clip cuts off, or lines spoken over each other. Cards whose morae are
off by more than `reading_check.MISMATCH` are counted apart (`clean` and `all` in the
table).

Writes `runs/<model>.words.jsonl` (each word with what the model heard, for the labelling) and
prints a table per model. `--combine` reads those files back and says what accepting a reading
only where every one of the given models hears it would do, which is the plan's rule, or where
`--quorum` of them do: the words that would go to review, and how often the models agree on a
draft the captions show to be wrong, the error that rule lets through.

    python word_array/research/furigana_audio_score.py [MODEL ...]
    python word_array/research/furigana_audio_score.py --combine MODEL MODEL [...] [--quorum K]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence

import furigana_audio as fa
from _bootstrap import load
from furigana_audio_run import MODELS, RUNS
from furigana_audio_select import jmdict_readings, sudachi_tokenizer

if TYPE_CHECKING:
    # `load()` gives a module object, so what comes out of it is untyped; the real module is
    # named for type checking only (the runtime import is what _bootstrap avoids).
    from japanese_note_ai_ops.word_array import reading_check as rc
else:
    rc = load("reading_check")

# A ruby base is the kanji before the reading: anything wider took the kana before them too,
# でも優[やさ] for やさ
RUBY_RE = re.compile(f"[{fa.KANJI}]+\\[([^\\]]*)\\]")
KINDS = ("name", "caption_word", "ambiguous", "number", "split")

# The check is the op's; the scripts and tests here reach these through this module
morae = rc.morae
align = rc.align
agrees = rc.agrees
sound = rc.sound
same_reading = rc.same_reading
hears = rc.hears


def heard_text(model: str, text: str, tokenize: Callable[[str], Sequence]) -> str:
    """The kana a model's answer gives: Ruby-ASR's ruby text keeps its readings and loses the
    kanji they read; the kana models' answers are kana already, marks aside. Ordinary text is
    read as Sudachi reads it, which says nothing of a word's reading where the model wrote the
    caption's kanji, and something where it wrote kana or other kanji (a name it does not
    know, a word the caption does not have)."""
    writes = MODELS[model].writes if model in MODELS else "kana"
    if writes == "ruby":
        return RUBY_RE.sub(lambda m: m.group(1), text)
    if writes == "text":
        return "".join(m for mo in tokenize(fa.kanji_numerals(text)) for m in rc.spoken(mo))
    return text


@dataclass
class WordResult:
    card: str
    word: dict
    expected: str  # the reading the line is aligned with: the caption's, else Sudachi's
    heard: str = ""
    verdicts: dict = field(default_factory=dict)
    # Whether the model hears the draft, and each reading the op may write instead
    op_verdicts: dict = field(default_factory=dict)
    clean: bool = True  # the card's transcript is close enough to its line (MISMATCH)
    gold: Optional[str] = None  # the reading known: the line's caption, else the show's name
    label: Optional[dict] = None  # the label given by ear, if any (fa.read_labels)

    def row(self) -> dict:
        w = self.word
        return {
            "id": self.card,
            "line": w["line"],
            "start": w["start"],
            "surface": w["surface"],
            "sudachi": w["sudachi"],
            "caption": w["caption"],
            "kinds": w["kinds"],
            "heard": self.heard,
            "clean": self.clean,
            "gold": self.gold,
            "gold_is_draft": same_reading(self.gold, w["sudachi"]) if self.gold else None,
            # Whether the model hears each reading: the draft's, the caption's, the show's
            "hears": self.verdicts,
            # The same for the readings the op chooses from: "draft", and each other reading
            # by its kana
            "op_hears": self.op_verdicts,
            "label": self.label["reading"] if self.label else None,
            "label_verdict": self.label["verdict"] if self.label else None,
        }


def reference(
    card: dict, tokenize: Callable[[str], Sequence]
) -> tuple[list[str], list[int], list[WordResult]]:
    """The card's spoken lines as morae, which word each mora belongs to (-1 for none), and
    the words (`reading_check.reference`). A kanji word is read as the caption reads it, else
    as Sudachi does."""
    words = card["words"]
    spans = [rc.Span(w["line"], w["start"], w["end"], w["caption"] or w["sudachi"]) for w in words]
    texts = [fa.split_readings(line)[0] for line in card["lines"]]
    ref, owner, placed = rc.reference(texts, spans, tokenize)
    return ref, owner, [WordResult(card["id"], words[k], spans[k].reading) for k in placed]


# A word's other readings in the dictionary, which the op may write instead of the draft
Alternatives = Callable[[dict], Iterable[str]]


def no_alternatives(word: dict) -> Iterable[str]:
    return ()


def score_card(
    card: dict,
    text: str,
    model: str,
    tokenize: Callable[[str], Sequence],
    show: dict,
    alternatives: Alternatives = no_alternatives,
    labels: Optional[dict] = None,
) -> tuple[list[WordResult], float, int]:
    """The card's words with what the model heard for each, the alignment's cost and the
    number of reference morae. A word labelled by ear (`labels`, fa.read_labels) also says
    whether the model heard the labelled reading rather than the draft."""
    ref, owner, words = reference(card, tokenize)
    hyp = morae(heard_text(model, text, tokenize))
    cost, pairs = align(ref, hyp)
    clean = cost <= rc.MISMATCH * max(len(ref), 1)
    spans: dict[int, list[int]] = defaultdict(list)
    for i, j in enumerate(pairs):
        if owner[i] >= 0 and j is not None:
            spans[owner[i]].append(j)
    for k, result in enumerate(words):
        js = spans.get(k)
        result.heard = "".join(hyp[min(js) : max(js) + 1]) if js else ""
        result.clean = clean
        lo, hi = rc.neighbours(owner, pairs, k, len(hyp))
        w = result.word
        readings = {"draft": w["sudachi"]}
        # What the op may write besides the draft: a reading the dictionary has for the spelling
        # and, for a name, the show's
        others = list(alternatives(w))
        if w["caption"]:
            readings["caption"] = w["caption"]
            result.gold = w["caption"]
        if w["surface"] in show:
            # Judged only where the pick took the word for one: 子 is a clan read シ in one
            # line, and the child こ everywhere else
            script, reading = show[w["surface"]]
            if (script == "name" and "name" in w["kinds"]) or (
                script == "word" and "caption_word" in w["kinds"]
            ):
                if not w["caption"]:
                    readings[f"show_{script}"] = reading
                if script == "name":
                    others.append(reading)
                    result.gold = result.gold or reading
        result.verdicts = hears(readings, hyp, lo, hi)
        result.op_verdicts = rc.hears_choices(rc.choices(w["sudachi"], others), hyp, lo, hi)
        key = (card["id"], w["line"], w["start"])
        result.label = fa.label_for(labels or {}, key, w["surface"])
        if result.label and result.label["verdict"] == "heard":
            said = {"label": result.label["reading"]}
            if not fa.KANJI_RE.search(w["sudachi"]):
                said["draft"] = w["sudachi"]
            result.verdicts["label"] = hears(said, hyp, lo, hi)["label"]
    return words, cost, len(ref)


def show_readings() -> dict[str, tuple[str, str]]:
    """surface -> (script, its most common caption reading) over the show, and the names given
    by ear (fa.read_names), which say what the captions never did, or otherwise."""
    out = {}
    for row in fa.read_jsonl(fa.CAPTION_READINGS):
        reading = max(row["readings"], key=row["readings"].get)
        out[row["surface"]] = ("name" if row["script"] == "katakana" else "word", reading)
    for surface, reading in fa.read_names().items():
        out[surface] = ("name", reading)
    return out


def dictionary_readings() -> Alternatives:
    """A word's JMdict readings, by its spelling as the line writes it, a number's digits as
    kanji numerals (10日 as 十日): a conjugated verb (言わ) is no spelling JMdict has, so it gets
    none and only its draft is written."""
    cache: dict[str, list[str]] = {}

    def readings(word: dict) -> list[str]:
        surface = word["surface"]
        if surface not in cache:
            forms = {surface, fa.kanji_numerals(surface)}
            cache[surface] = sorted(set().union(*(jmdict_readings(f) for f in forms)))
        return cache[surface]

    return readings


def rate(results: Sequence[WordResult], key: str, test: Callable[[WordResult], bool]) -> str:
    """The share of the words `test` picks that agree with the `key` reading: on the cards
    whose transcript matches their line, then on all."""
    out = []
    for only_clean in (True, False):
        chosen = [
            r for r in results if test(r) and key in r.verdicts and (r.clean or not only_clean)
        ]
        hits = sum(r.verdicts[key] for r in chosen)
        out.append(f"{100 * hits / len(chosen):5.1f}% of {len(chosen):<4}" if chosen else "-" * 14)
    return "   ".join(out)


def report(
    model: str, results: Sequence[WordResult], cost: float, ref_len: int, secs: Sequence[float]
) -> str:
    def caption_differs(r: WordResult) -> bool:
        caption = r.word["caption"]
        return bool(caption) and not same_reading(caption, r.word["sudachi"])

    def caption_same(r: WordResult) -> bool:
        return bool(r.word["caption"]) and same_reading(r.word["caption"], r.word["sudachi"])

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

    def labelled(as_draft: bool) -> Callable[[WordResult], bool]:
        return lambda r: (
            "label" in r.verdicts
            and r.label is not None
            and same_reading(r.label["reading"], r.word["sudachi"]) == as_draft
        )

    if any("label" in r.verdicts for r in results):
        rows += [
            ("labelled, said otherwise than Sudachi", "label", labelled(False), "the label's"),
            ("labelled, said as Sudachi reads it", "label", labelled(True), "the label's"),
        ]
    mean = sum(secs) / max(len(secs), 1)
    cards = {r.card: r.clean for r in results}
    lines = [
        f"== {model}: {len(secs)} cards, {mean:.1f} s a card,"
        f" morae off {100 * cost / max(ref_len, 1):.1f}% of the line as read;"
        f" {sum(cards.values())} cards match their line",
        f"  {'':<36} {'':<19} {'clean cards':<17}   all cards",
    ]
    for label, key, test, whose in rows:
        lines.append(f"  {label:<36} hears {whose:<13} {rate(results, key, test)}")
    return "\n".join(lines)


def percent(n: int, total: int) -> str:
    return f"{100 * n / max(total, 1):.1f}%"


def decide(rows: Sequence[dict], quorum: int) -> Optional[str]:
    """The reading the op writes for a word, given each model's row of it, or None for a
    review (`reading_check.decide`)."""
    return rc.decide([r["op_hears"] for r in rows], rows[0]["sudachi"], quorum)


# Why a word is written as it is, or not: the rule writes another reading than the draft, sends
# the word to review, or writes the draft that a quorum but not every model heard, or every one
BUCKETS = ("correction", "review", "partial", "agreed")


def outcome(rows: Sequence[dict], quorum: int) -> tuple[Optional[str], str]:
    """What the op writes for a word, given each model's row of it, and its bucket."""
    written = decide(rows, quorum)
    if written is None:
        return None, "review"
    if written != rows[0]["sudachi"]:
        return written, "correction"
    votes = sum(r["op_hears"]["draft"] for r in rows)
    return written, "agreed" if votes == len(rows) else "partial"


def combine(models: Sequence[str], quorum: Optional[int] = None) -> str:
    """What the op would write over the words of the cards whose transcript matches the line
    in every run, accepting a reading where `quorum` of the models hear it (all, by default):
    the draft, or a reading the dictionary has for the spelling or the show has for a name
    (`decide`). Anything else goes to review, a reading a quorum hears that nothing licenses
    included: the English line or a person has to settle it.

    Where the reading is known (a label given by ear, else the line's caption or the show's
    for a name) a written reading is right or wrong, its vowels' length included
    (`same_reading`); that is the error the rule lets through.
    Elsewhere the draft is usually right, so a word written otherwise is counted apart, to be
    checked by hand. A word labelled "can't tell" or "not said" has no known reading. The
    labelled words are also counted by why the rule wrote them as it did (`outcome`), which is
    how the queue picked them: the error rate of each bucket.

    The random stratum's cards are the ones like a show's other lines, so the share of them
    with a word to review is the review an op would make; the other strata were picked for
    their hard words."""
    k = quorum or len(models)
    tables = []
    for m in models:
        rows = fa.read_jsonl(RUNS / f"{m}.words.jsonl")
        if not rows or "op_hears" not in rows[0]:
            return f"{m}: score it first (furigana_audio_score.py {m})"
        tables.append({(r["id"], r["line"], r["start"]): r for r in rows if r["clean"]})
    keys = sorted(set.intersection(*(set(t) for t in tables)))
    labels = fa.read_labels()
    counts: dict[str, int] = defaultdict(int)
    by_ear: dict[str, int] = defaultdict(int)
    review: list[tuple] = []
    for key in keys:
        rows = [t[key] for t in tables]
        first = rows[0]
        written, bucket = outcome(rows, k)
        label = fa.label_for(labels, key, first["surface"])
        said = label["reading"] if label and label["verdict"] == "heard" else None
        gold = said if label else first["gold"]
        if label and said is None:
            by_ear[label["verdict"]] += 1
        elif said and written is None:
            as_draft = same_reading(said, first["sudachi"])
            by_ear[f"review: {'as' if as_draft else 'not as'} the draft"] += 1
        elif said and written is not None:
            by_ear[f"{bucket}: {'right' if same_reading(written, said) else 'wrong'}"] += 1
        if gold:
            group = "known, Sudachi " + (
                "right" if same_reading(gold, first["sudachi"]) else "wrong"
            )
            right = written is not None and same_reading(written, gold)
            result = "review" if written is None else "right" if right else "wrong"
        else:
            group = "ordinary" if not first["kinds"] else "other hard"
            result = (
                "review"
                if written is None
                else "draft" if written == first["sudachi"] else "other reading"
            )
            if written is None and all(
                r["heard"] and agrees(first["heard"], r["heard"]) for r in rows
            ):
                counts[f"{group}: one unlicensed reading"] += 1
        if written is None:
            review.append(key)
        counts[group] += 1
        counts[f"{group}: {result}"] += 1
    rule = "every model" if k == len(models) else f"{k} of {len(models)}"
    lines = [
        f"== {' + '.join(models)}, {rule}: {len(keys)} words of"
        f" {len({key[0] for key in keys})} cards clean in every run"
    ]
    for group, outcomes in (
        ("known, Sudachi wrong", ("right", "wrong", "review")),
        ("known, Sudachi right", ("right", "wrong", "review")),
        ("ordinary", ("draft", "other reading", "review")),
        ("other hard", ("draft", "other reading", "review")),
    ):
        total = counts[group]
        parts = [f"{o} {percent(counts[f'{group}: {o}'], total)}" for o in outcomes]
        unlicensed = counts.get(f"{group}: one unlicensed reading")
        if unlicensed:
            parts[-1] += f" ({percent(unlicensed, total)} one reading nothing licenses)"
        lines.append(f"  {group:<22} {total:>5} words: " + ", ".join(parts))
    stratum = {c["id"]: c["stratum"] for c in fa.read_jsonl(fa.SELECTION)}
    for name, pick in (("all", None), ("random", "random")):
        mine = [key for key in keys if pick in (None, stratum.get(key[0]))]
        cards = {key[0] for key in mine}
        flagged = [key for key in review if key[0] in cards]
        flagged_cards = {key[0] for key in flagged}
        lines.append(
            f"  {name + ' cards:':<14} {len(flagged_cards)} of {len(cards)}"
            f" ({100 * len(flagged_cards) / max(len(cards), 1):.0f}%) have a word to review,"
            f" {len(flagged)} of {len(mine)} words"
            f" ({100 * len(flagged) / max(len(mine), 1):.1f}%)"
        )
    if by_ear:
        lines.append(labelled_lines(by_ear))
    return "\n".join(lines)


def labelled_lines(by_ear: dict[str, int]) -> str:
    """The labelled words by the rule's bucket: of those it wrote, how many are right, and of
    those it sent to review, how many were said as the draft has it."""
    total = sum(by_ear.values())
    unclear = by_ear.get("unsure", 0) + by_ear.get("not_said", 0)
    lines = [
        f"  labelled by ear: {total} words, of them {by_ear.get('unsure', 0)} can't tell and"
        f" {by_ear.get('not_said', 0)} not said"
    ]
    for bucket in BUCKETS:
        if bucket == "review":
            as_draft, other = by_ear["review: as the draft"], by_ear["review: not as the draft"]
            if as_draft + other:
                lines.append(
                    f"    {bucket:<11} {as_draft + other:>4}: said as the draft {as_draft},"
                    f" otherwise {other}"
                )
            continue
        right, wrong = by_ear[f"{bucket}: right"], by_ear[f"{bucket}: wrong"]
        if right + wrong:
            # With no error among n, an error rate above 3/n would show one 95% of the time;
            # under 30 that says nothing worth printing
            bound = f", under {percent(3, right)} wrong at 95%" if not wrong and right >= 30 else ""
            lines.append(
                f"    {bucket:<11} {right + wrong:>4} written: right {right}, wrong {wrong}{bound}"
            )
    if total == unclear:
        lines.append("    no reading heard yet")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("models", nargs="*", help="the runs to score (default: every run there is)")
    ap.add_argument("--combine", action="store_true", help="the models' runs together")
    ap.add_argument(
        "--quorum", type=int, help="with --combine: how many models must hear a reading (all)"
    )
    args = ap.parse_args(argv)
    if args.combine:
        print(combine(args.models, args.quorum))
        return 0
    models = args.models or sorted(
        p.stem for p in RUNS.glob("*.jsonl") if not p.stem.endswith(".words")
    )
    cards = {c["id"]: c for c in fa.read_jsonl(fa.SELECTION)}
    tokenize = sudachi_tokenizer()
    show = show_readings()
    alternatives = dictionary_readings()
    labels = fa.read_labels()
    for model in models:
        rows = fa.read_jsonl(RUNS / f"{model}.jsonl")
        results: list[WordResult] = []
        total_cost, total_ref = 0.0, 0
        for row in rows:
            if row["id"] not in cards:
                continue
            words, cost, ref_len = score_card(
                cards[row["id"]], row["text"], model, tokenize, show, alternatives, labels
            )
            results.extend(words)
            total_cost += cost
            total_ref += ref_len
        fa.write_jsonl(RUNS / f"{model}.words.jsonl", (r.row() for r in results))
        print(report(model, results, total_cost, total_ref, [r["seconds"] for r in rows]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
