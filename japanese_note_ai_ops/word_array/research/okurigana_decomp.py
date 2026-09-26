"""Decompositions whose second piece is kana only (窪+み, 赤+ちゃん), with what could tell okurigana
from a word: whether the whole is a verb's ます-stem or an adjective form, the tail's JMdict codes.

    python word_array/research/okurigana_decomp.py [--corpus export] [-n COUNT] [--out FILE]
"""

import argparse
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load, load_root
from corpora import CORPORA, read_export

generator = load("generator")
jmdict = load("jmdict_index")
html_stripping = load_root("html_stripping")


def features(written: str, first: str, tail: str) -> str:
    out = []
    stem = generator.VERB_STEM_TO_DICT.get(written[-1:])
    if stem and jmdict.has_pos(written[:-1] + stem, "v5"):
        out.append("v5")
    if jmdict.has_pos(written + "る", "v1"):
        out.append("v1")
    if written[-1:] in "みさき" and jmdict.has_pos(written[:-1] + "い", "adj-i"):
        out.append("adj")
    if generator.KANJI_RE.search(first[-1:]):
        out.append("after-kanji")
    codes = sorted({p for _, rs, ps in jmdict.lookup(tail) if tail in rs for p in ps})
    return " ".join(out) + " | " + ",".join(codes)


def walk(tm, words, hits: Counter, samples: dict) -> None:
    for w in words:
        if w.kind == "word" and len(w.subs) == 2:
            first, second = w.subs
            tail = tm.written_form(second.start, second.end)
            if not generator.KANJI_RE.search(tail):
                written = tm.written_form(w.start, w.end)
                head = f"{w.head.pos[0]}/{w.head.pos[1]}"
                key = (head, tm.written_form(first.start, first.end), tail, written)
                hits[key] += 1
                samples.setdefault(key, features(written, key[1], tail))
        walk(tm, w.subs, hits, samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "okurigana_decomp_report.txt"))
    args = parser.parse_args()
    hits: Counter = Counter()
    samples: dict = {}
    for raw, _ in read_export(CORPORA[args.corpus], Counter())[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if sentence.strip():
            analysis = generator.analyze(sentence)
            walk(analysis.text_map, analysis.final, hits, samples)
    by_head: dict = defaultdict(list)
    for key, n in hits.items():
        by_head[key[0]].append((n, key))
    lines = [f"{sum(hits.values())} hits, {len(hits)} distinct"]
    for head in sorted(by_head, key=lambda h: -sum(n for n, _ in by_head[h])):
        lines += ["", f"== {head}"]
        for n, key in sorted(by_head[head], reverse=True):
            lines.append(f"{n:4} {key[1]} + {key[2]}  ({samples[key]})")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(lines[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
