"""Names the generator cuts into: where a proper noun starts or ends inside a top-level word.

`proper_noun_llm.fix_array` only merges names that start and end on word boundaries. This takes the
old lists' `proper_nouns` as the names (no requests) over a corpus, and for every name off the
boundaries reports the word it cuts, its sub-words, and what is left of the word beside the name:
whether the name is already a sub-word of it (江戸 of 江戸時代), and what the rest generates as on
its own (と a particle, 少年 a JMdict noun, 語 a suffix). SPLIT lines are the sentences where
`generator.name_rest_word` lets `fix_array` split a cut word, shown as the fixed array.

    py -3.10 word_array/research/name_boundaries.py [--corpus export] [--limit N]
"""

import argparse
from collections import Counter

import proper_nouns as survey
from _bootstrap import ADDON_ROOT, load
from migrate_fit import CORPORA, read_export

gen = survey.generator
pn = load("proper_noun_llm")
OUT = ADDON_ROOT / "output" / "name_boundaries_report.txt"


def show(e: list) -> str:
    subs = " + ".join(f"{s[2]}[{s[3]}]<{s[1]}>" for s in e[5] if len(s) > 1)
    return f"{e[2]}[{e[3]}]<{e[1]}>" + (f" {{{subs}}}" if subs else "")


def rest_kind(rest: str) -> str:
    """What the part of a word beside a name is when generated alone."""
    arr = [e for e in gen.generate(rest) if len(e) > 1]
    if len(arr) != 1:
        return f"{len(arr)} words"
    e = arr[0]
    jm = bool(gen.jmdict.lookup(e[2]))
    return f"{e[1]}{' jmdict' if jm else ''}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    kinds: Counter = Counter()
    lines: list[str] = []
    for n, (raw, word_lists) in enumerate(read_export(CORPORA[args.corpus], Counter())):
        if args.limit and n >= args.limit:
            break
        sentence = survey.html_stripping.strip_context_sentences(raw)
        old, _ = survey.migrate.read_word_lists(word_lists)
        names = sorted({e.word for e in old if e.category == "proper_nouns" and e.word})
        if not sentence.strip() or not names:
            continue
        arr = gen.generate(sentence)
        split = [list(e) for e in arr]
        kinds["split"] += len(set(pn.fix_array(split, names, gen.name_rest_word).changed))
        unsplit = [list(e) for e in arr]
        kinds["split"] -= len(set(pn.fix_array(unsplit, names).changed))
        if split != unsplit:
            words = [f"{e[2]}<{e[1]}>" for e in split if len(e) > 1]
            lines.append(f"SPLIT {' | '.join(words)}")
        fix = pn.fix_array([list(e) for e in arr], names)
        plain = "".join(pn.plain_text(e[0]) for e in arr)
        spans = pn._spans(arr)
        for name in fix.unaligned:
            at = plain.find(name)
            if at < 0:
                kinds["not in sentence"] += 1
                continue
            end = at + len(name)
            cut = [(e, s, t) for e, (s, t) in zip(arr, spans) if len(e) > 1 and s < end and t > at]
            if len(cut) != 1:
                kinds["across words"] += 1
                lines.append(f"ACROSS {name} :: " + " || ".join(show(e) for e, _, _ in cut))
                continue
            e, s, t = cut[0]
            text = pn.plain_text(e[0])
            if s == at:
                side, rest = "starts", text[end - s :]
            elif t == end:
                side, rest = "ends", text[: at - s]
            else:
                kinds["inside"] += 1
                lines.append(f"INSIDE {name} :: {show(e)}")
                continue
            is_sub = any(len(x) > 1 and pn.plain_text(x[0]).strip() == name for x in e[5])
            kind = "name is a sub-word" if is_sub else rest_kind(rest.strip())
            kinds[f"{side} a word, rest {kind}"] += 1
            lines.append(f"{side.upper()} {name} + {rest.strip()} ({kind}) :: {show(e)}")
    head = [f"{v:5} {k}" for k, v in kinds.most_common()]
    OUT.write_text("\n".join(head + [""] + sorted(lines)) + "\n", encoding="utf-8")
    print("\n".join(head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
