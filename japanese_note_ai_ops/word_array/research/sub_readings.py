"""Parents whose sub-words' readings don't add up to the parent's own reading.

Readings compared are what the text reads each word as: the note's furigana, or Sudachi's reading
where the note gives none (`generator._furigana_reading`). A sub-word after the first may carry
rendaku either way.

    python word_array/research/sub_readings.py [--corpus export] [-n COUNT] [--limit 60]
"""

import argparse
import itertools
from collections import Counter

from _bootstrap import load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
html_stripping = load_root("html_stripping")


def adds_up(parent: str, subs: list[str]) -> bool:
    options = [[subs[0]]] + [
        dict.fromkeys([s, generator.voiced(s), generator.unvoiced(s)]) for s in subs[1:]
    ]
    return any("".join(c) == parent for c in itertools.product(*options))


def walk(tm, words, counts: Counter, examples: Counter) -> None:
    for w in words:
        if len(w.subs) >= 2:
            reading = generator._furigana_reading(tm, w.start, w.end, w.morphs)
            subs = [generator._furigana_reading(tm, s.start, s.end, s.morphs) for s in w.subs]
            furigana = not generator.KANJI_RE.search(tm.surface_reading(w.start, w.end))
            counts["parents", furigana] += 1
            if not adds_up(reading, subs):
                counts["bad", furigana] += 1
                pieces = " + ".join(
                    f"{tm.written_form(s.start, s.end)}[{r}]" for s, r in zip(w.subs, subs)
                )
                examples[furigana, tm.written_form(w.start, w.end), reading, pieces] += 1
        walk(tm, w.subs, counts, examples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--limit", type=int, default=60, help="examples to print")
    args = parser.parse_args()
    rows = read_export(CORPORA[args.corpus], Counter())
    counts: Counter = Counter()
    examples: Counter = Counter()
    for raw, _ in rows[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if sentence.strip():
            analysis = generator.analyze(sentence)
            walk(analysis.text_map, analysis.final, counts, examples)
    for furigana in (True, False):
        print(
            f"furigana={furigana}: {counts['bad', furigana]} of {counts['parents', furigana]}"
            " parents don't add up"
        )
    for key, n in examples.most_common(args.limit):
        print(n, *key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
