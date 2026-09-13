"""JMdict matches found only by their kana where the text writes kanji (成ると read as 鳴門 なると),
not starting on a particle: the ones whose entries JMdict spells otherwise, by form, and the ones
JMdict spells alike, for comparison.

    python word_array/research/kana_matches.py [--corpus export] [-n COUNT] [--out FILE]
"""

import argparse
from collections import Counter

from _bootstrap import ADDON_ROOT, load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
html_stripping = load_root("html_stripping")

KANJI_RE = generator.KANJI_RE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "kana_matches_report.txt"))
    args = parser.parse_args()
    unlike: Counter = Counter()
    kana_only: Counter = Counter()
    alike: Counter = Counter()
    for raw, _ in read_export(CORPORA[args.corpus], Counter())[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if not sentence.strip():
            continue
        an = generator.analyze(sentence)
        tm, words = an.text_map, an.words
        for c in an.candidates:
            if KANJI_RE.search(c.form) or not KANJI_RE.search(c.written):
                continue
            ws = words[c.i : c.j]
            if ws[0].head.pos[0] == "助詞" or all(
                w.head.pos[0] in generator.FUNCTION_POS for w in ws
            ):
                continue
            parts = " + ".join(tm.written_form(w.start, w.end) for w in ws)
            key = (c.form, parts, ",".join(sorted(c.pos)), "/".join(c.spellings[:4]))
            if not c.spellings:
                kana_only[key] += 1
            elif any(generator.spelled_alike(c.written, s) for s in c.spellings):
                alike[key] += 1
            else:
                unlike[key] += 1
    lines = [
        f"spelled otherwise {sum(unlike.values())} ({len(unlike)} distinct), kana-only entries "
        f"{sum(kana_only.values())}, spelled alike {sum(alike.values())}"
    ]
    for title, hits in (("spelled otherwise", unlike), ("kana-only", kana_only), ("alike", alike)):
        lines += ["", f"== {title}"]
        for key, n in hits.most_common():
            lines.append(f"{n:4} {key[0]} = {key[1]}  [{key[2]}] {key[3]}")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(lines[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
