"""Word raw_texts that aren't balanced html: a tag closed inside the word it wasn't opened in
(`済[す]み</k>ません`), or opened and left open (`<k>為[し]`). Most come from `<k>` put around
only part of a kanjified word in the data (`<k>為[し]</k>ます`), so the report also lists
sentences whose markup is plainly broken, for fixing in the collection.

    python word_array/research/unbalanced_tags.py [--corpus export] [-n COUNT] [--out FILE]
"""

import argparse
import re
from collections import Counter

from _bootstrap import ADDON_ROOT, load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
html_stripping = load_root("html_stripping")

MARKUP_SLIPS = [
    ("tag inside furigana", re.compile(r"\[[^\]<]*<[^\]]*\]")),
    ("empty tag pair", re.compile(r"<([bk])></\1>")),  # an empty <div></div> is the editor's
    ("same tag nested", re.compile(r"<([bk])>(?:(?!</\1>).)*<\1>")),
    ("garbled tag", re.compile(r"<[^>a-z/][^>]*/[a-z]+>")),
    (
        "<k> closed before the inflection",
        re.compile(r"\][りきしちにみいえけせてべめれげ]?</k>(ま[すせし]|たい|ながら)"),
    ),
]


def imbalance(raw: str) -> str:
    """Empty when balanced, else "close" (stray closing tag), "open" (unclosed) or "both"."""
    opened, stray = [], False
    for m in generator.OPEN_CLOSE_TAG_RE.finditer(raw):
        if not m.group(1):
            opened.append(m.group(2))
        elif opened and opened[-1] == m.group(2):
            opened.pop()
        else:
            stray = True
    return {(False, False): "", (True, False): "close", (False, True): "open"}.get(
        (stray, bool(opened)), "both"
    )


def walk(arr: list, depth: int, hits: Counter, samples: dict, sentence: str) -> None:
    for elem in arr:
        if len(elem) < 6:
            continue
        kind = imbalance(elem[0])
        if kind:
            key = (kind, "nested" if depth else "top")
            hits[key] += 1
            samples.setdefault(key, []).append(f"{elem[0]}  <=  {sentence}")
        walk(elem[5], depth + 1, hits, samples, sentence)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "unbalanced_tags_report.txt"))
    args = parser.parse_args()
    hits: Counter = Counter()
    samples: dict = {}
    slips: dict = {label: [] for label, _ in MARKUP_SLIPS}
    rows = 0
    for raw, _ in read_export(CORPORA[args.corpus], Counter())[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if sentence.strip():
            rows += 1
            walk(generator.generate(sentence), 0, hits, samples, sentence)
            for label, pattern in MARKUP_SLIPS:
                if pattern.search(sentence):
                    slips[label].append(sentence)
    lines = [f"{rows} sentences, {sum(hits.values())} unbalanced raw_texts"]
    for key, n in hits.most_common():
        lines += ["", f"== {key[0]} / {key[1]}: {n}"] + samples[key][:40]
    for label, found in slips.items():
        lines += ["", f"== markup slip, {label}: {len(found)}"] + found
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(line for line in lines if line.startswith(("==", str(rows)))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
