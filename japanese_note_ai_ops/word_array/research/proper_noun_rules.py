"""What per-sentence proper noun rules would change: top-level words where Sudachi says 固有名詞 but
a JMdict match labels them (rule 1), out-of-vocabulary nouns (rule 2), katakana runs joined by ・
(rule 3), and Sudachi proper nouns the note's furigana reads otherwise (rule 4).

    python word_array/research/proper_noun_rules.py [-n COUNT] [--out FILE]
"""

import argparse
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
migrate = load("migrate")
html_stripping = load_root("html_stripping")

SAMPLES = 40
KATA = set(chr(c) for c in range(ord("ァ"), ord("ヶ") + 1)) | {"ー"}


def is_kata(s: str) -> bool:
    return bool(s) and all(c in KATA for c in s)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=0)
    parser.add_argument(
        "--out", default=str(ADDON_ROOT / "output" / "proper_noun_rules_report.txt")
    )
    args = parser.parse_args()
    found: dict = defaultdict(Counter)
    old_proper: dict = defaultdict(Counter)
    for raw, word_lists in read_export(CORPORA["export"], Counter())[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if not sentence.strip():
            continue
        a = generator.analyze(sentence)
        tm = a.text_map
        old, _ = migrate.read_word_lists(word_lists)
        named = {e.word for e in old if e.category == "proper_nouns"}
        oov = {
            (m.begin(), m.end())
            for m in generator._tokenizer().tokenize(tm.natural, generator.SplitMode.C)
            if m.is_oov()
        }
        for w in a.final:
            if w.kind == "punct":
                continue
            h = w.head
            form = generator.dict_form(tm, w)
            reading = generator.dict_reading(tm, w)
            tag = "named" if form in named or h.surface in named else "not named"
            if any(m.pos[:2] == ("名詞", "固有名詞") for m in w.morphs):
                label = generator.pos_label(tm, w, None, reading)
                if label != "proper noun":
                    key = f"{len(w.morphs)} morphs -> {label}"
                    found["1 has a proper noun morph, labelled: " + key][
                        f"{form}[{reading}] ({tag})"
                    ] += 1
            if len(w.morphs) != 1:
                continue
            if h.pos[:2] == ("名詞", "固有名詞") and reading != h.reading:
                entries = generator.jmdict.lookup(form)
                hira = [{generator.to_hiragana(r) for r in rs} for _, rs, _ in entries]
                if any(reading in rs and h.reading in rs for rs in hira):
                    kind = "same entry has both"
                elif any(reading in rs for rs in hira):
                    kind = "jmdict word by furigana"
                else:
                    kind = "no jmdict word by furigana"
                found[f"4 proper noun read otherwise: {kind}"][
                    f"{form}[{reading}] sudachi {h.reading} ({tag})"
                ] += 1
            if (h.start, h.end) in oov and h.pos[:2] == ("名詞", "普通名詞") and is_kata(h.surface):
                in_jm = bool(generator.jmdict.lookup(h.surface))
                size = "len>=3" if len(h.surface) >= 3 else "short"
                found[f"2 oov katakana noun, jmdict {in_jm}, {size}"][
                    f"{form}[{reading}] ({tag})"
                ] += 1
        # rule 3: katakana word, ・, katakana word (, ・, ...)
        final = a.final
        i = 0
        while i < len(final):
            j = i
            parts = []
            while j < len(final) and final[j].kind != "punct" and is_kata(final[j].natural):
                parts.append(final[j])
                if (
                    j + 2 < len(final)
                    and final[j + 1].kind == "punct"
                    and final[j + 1].natural in ("・", "･")
                    and is_kata(final[j + 2].natural)
                ):
                    j += 2
                else:
                    j += 1
                    break
            if len(parts) > 1:
                text = "・".join(p.natural for p in parts)
                labels = "+".join(
                    p.head.pos[1] if p.head.pos[0] == "名詞" else p.head.pos[0] for p in parts
                )
                tag = (
                    "named"
                    if any(
                        text.replace("・", "") in n.replace("・", "") or n in text for n in named
                    )
                    else "not named"
                )
                name_like = any(
                    p.head.pos[:2] == ("名詞", "固有名詞") or not generator.jmdict.lookup(p.natural)
                    for p in parts
                )
                found[f"3 katakana・katakana, a part proper/not jmdict {name_like}"][
                    f"{text} {labels} ({tag})"
                ] += 1
                i = j
            else:
                i += 1
    lines = []
    for key in sorted(found):
        c = found[key]
        named_n = sum(n for k, n in c.items() if k.endswith("(named)"))
        lines += ["", f"== {key}: {sum(c.values())} (old list proper noun {named_n})"]
        lines += [f"  {n:4} {k}" for k, n in c.most_common(SAMPLES)]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(ln for ln in lines if ln.startswith("== ")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
