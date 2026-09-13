"""Nouns the generator lists as the verb they are the stem of (嫌い -> 嫌う, 動き -> 動く), by how
JMdict has the noun itself and by what the old word list of the sentence kept.

JMdict classes of the noun, spelled and read as in the text: `adj-na` (嫌い, 好き), `n` (動き,
周り), `none` (no entry of its own: 買い of 買い物). `common` marks a kanji spelling JMdict gives a
priority tag (news1, ichi1, spec1, gai1). The old list kept the noun, the verb, both or neither.

    python word_array/research/noun_stems.py [--corpus export] [-n COUNT] [--out FILE]
"""

import argparse
import gzip
import re
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
migrate = load("migrate")
jmdict = load("jmdict_index")
html_stripping = load_root("html_stripping")

PRIORITY = {"news1", "ichi1", "spec1", "gai1"}
ENTRY_RE = re.compile(r"<entry>.*?</entry>", re.S)
KEB_RE = re.compile(r"<k_ele>\s*<keb>([^<]+)</keb>(.*?)</k_ele>", re.S)


def common_kebs() -> set[str]:
    text = gzip.open(jmdict.JMDICT_GZ, "rt", encoding="utf-8").read()
    out = set()
    for entry in ENTRY_RE.finditer(text):
        for keb, rest in KEB_RE.findall(entry.group(0)):
            if any(f"<ke_pri>{p}</ke_pri>" in rest for p in PRIORITY):
                out.add(keb)
    return out


def jm_class(noun: str, reading: str) -> str:
    pos = set()
    for kebs, rebs, ps in jmdict.lookup(noun):
        if noun in kebs and reading in {generator.to_hiragana(r) for r in rebs}:
            pos |= ps
    if "adj-na" in pos:
        return "adj-na"
    if any(p == "n" or p.startswith("n-") for p in pos):
        return "n"
    return "other" if pos else "none"


def walk(tm, words, old: set[str], hits: list, parent: str = "") -> None:
    for w in words:
        verb = generator._noun_form_verb(tm, w) if w.kind == "word" else None
        if verb and generator.dict_form(tm, w) == verb:
            noun = tm.written_form(w.start, w.end)
            reading = generator.to_hiragana(
                generator._furigana_reading(tm, w.start, w.end, w.morphs)
            )
            kept_as = (
                "both"
                if noun in old and verb in old
                else "noun" if noun in old else "verb" if verb in old else "neither"
            )
            hits.append((noun, reading, verb, parent, kept_as))
        walk(tm, w.subs, old, hits, tm.written_form(w.start, w.end))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "noun_stems_report.txt"))
    args = parser.parse_args()
    rows = read_export(CORPORA[args.corpus], Counter())
    hits: list = []
    for raw, word_lists in rows[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if not sentence.strip():
            continue
        entries, _ = migrate.read_word_lists(word_lists)
        old = {e.word for e in entries}
        analysis = generator.analyze(sentence)
        walk(analysis.text_map, analysis.final, old, hits)
    common = common_kebs()

    by_word: dict[tuple, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    for noun, reading, verb, parent, kept_as in hits:
        cls = jm_class(noun, reading)
        key = (cls, noun in common, noun, reading, verb)
        by_word[key]["top" if not parent else "nested"] += 1
        by_word[key][kept_as] += 1
        totals[cls, noun in common, "nested" if parent else "top"] += 1
        totals[cls, noun in common, kept_as] += 1

    lines = [f"{len(hits)} hits, {len(by_word)} distinct (noun, reading, verb)", ""]
    lines.append("class common | top nested | old list kept: noun verb both neither | distinct")
    for cls in ("adj-na", "n", "other", "none"):
        for com in (True, False):
            t = [totals[cls, com, k] for k in ("top", "nested", "noun", "verb", "both", "neither")]
            distinct = sum(1 for k in by_word if k[:2] == (cls, com))
            lines.append(
                f"{cls:6} {com!s:5} | {t[0]} {t[1]} | {t[2]} {t[3]} {t[4]} {t[5]} | {distinct}"
            )
    for cls in ("adj-na", "n", "other", "none"):
        for com in (True, False):
            keys = sorted(
                (k for k in by_word if k[:2] == (cls, com)),
                key=lambda k: -(by_word[k]["top"] + by_word[k]["nested"]),
            )
            lines += ["", f"== {cls} common={com} ({len(keys)})"]
            for k in keys:
                c = by_word[k]
                lines.append(
                    f"{c['top'] + c['nested']:4} {k[2]}[{k[3]}] -> {k[4]}  top {c['top']} nested"
                    f" {c['nested']} | old noun {c['noun']} verb {c['verb']} both {c['both']}"
                    f" neither {c['neither']}"
                )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
