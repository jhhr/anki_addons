"""Score the generator against the gold arrays.

Spans are compared in plain written-text coordinates (tags, furigana and spaces removed), so raw
differences like tag placement don't hide agreement. "Reachable" counts gold words that exist
anywhere the rule stages produced them (words, sub-words, every JMdict candidate including the
ones the structural filters refused): what the rules could produce with different filters.

    python word_array/research/evaluate.py [-q]
"""

import sys
from collections import Counter

import gold
from _bootstrap import load

generator = load("generator")

NON_WORD_POS = {"non-word", "punctuation"}


def spans(arr: list, base: int = 0, depth: int = 0, out: list | None = None) -> list:
    """(depth, start, end, element) for word elements, recursing into sub_words."""
    out = [] if out is None else out
    pos = base
    for e in arr:
        n = len(gold.strip_to_plain(e[0]))
        if len(e) > 1 and e[1] not in NON_WORD_POS and n:
            out.append((depth, pos, pos + n, e))
            if len(e) >= 6 and e[5]:
                spans(e[5], pos, depth + 1, out)
        pos += n
    return out


def written_span(tm, start: int, end: int) -> tuple[int, int]:
    rs, re_ = tm.raw_span(start, end)
    s = len(gold.strip_to_plain(tm.raw[:rs]))
    return s, s + len(gold.strip_to_plain(tm.raw[rs:re_]))


def reachable_spans(an) -> set[tuple[int, int]]:
    tm, words = an.text_map, an.words
    out = set()

    def add_word(w):
        out.add(written_span(tm, w.start, w.end))
        for s in w.subs:
            add_word(s)

    for w in words:
        add_word(w)
    for c in an.candidates:
        out.add(written_span(tm, words[c.i].start, words[c.j - 1].end))
    return out


def main(verbose: bool) -> None:
    tot: Counter[str] = Counter()
    lemma_miss: list[tuple] = []
    reading_miss: list[tuple] = []
    for num, (sentence, gold_arr) in sorted(gold.load().items()):
        an = generator.analyze(sentence)
        plain = gold.strip_to_plain(an.text_map.raw)
        g_all, s_all = spans(gold_arr), spans(an.array)
        g_top = {(s, e): el for d, s, e, el in g_all if d == 0}
        s_top = {(s, e): el for d, s, e, el in s_all if d == 0}
        g_sub = {(s, e) for d, s, e, _ in g_all if d > 0}
        s_sub = {(s, e) for d, s, e, _ in s_all if d > 0}
        reach = reachable_spans(an) | {(s, e) for _, s, e, _ in s_all}

        hit = g_top.keys() & s_top.keys()
        tot["gold_top"] += len(g_top)
        tot["sys_top"] += len(s_top)
        tot["top_hit"] += len(hit)
        tot["top_reach"] += len(g_top.keys() & reach)
        tot["gold_sub"] += len(g_sub)
        tot["sys_sub"] += len(s_sub)
        tot["sub_hit"] += len(g_sub & s_sub)
        tot["sub_reach"] += len(g_sub & reach)
        for k in hit:
            ge, se = g_top[k], s_top[k]
            tot["lemma_ok"] += ge[2] == se[2]
            tot["reading_ok"] += ge[3] == se[3]
            tot["flags_ok"] += ge[4] == se[4]
            if ge[2] != se[2]:
                lemma_miss.append((num, ge[2], se[2]))
            if ge[3] != se[3]:
                reading_miss.append((num, ge[2], ge[3], se[3]))

        if verbose:
            miss = sorted(g_top.keys() - s_top.keys())
            extra = sorted(s_top.keys() - g_top.keys())
            if miss or extra:
                print(f"\n#{num} {plain}")
                print("  gold only:", [plain[s:e] for s, e in miss])
                print("  generated only:", [plain[s:e] for s, e in extra])
                unreach = sorted(g_top.keys() - reach)
                if unreach:
                    print("  gold words not reachable:", [plain[s:e] for s, e in unreach])
            sub_miss, sub_extra = sorted(g_sub - s_sub), sorted(s_sub - g_sub)
            if sub_miss or sub_extra:
                print(f"  #{num} sub-words gold only:", [plain[s:e] for s, e in sub_miss])
                print(f"  #{num} sub-words generated only:", [plain[s:e] for s, e in sub_extra])

    def pct(a: str, b: str) -> str:
        return f"{tot[a]}/{tot[b]} = {tot[a] / tot[b]:.1%}"

    print("\n=== summary ===")
    print(
        f"top-level exact span: recall {pct('top_hit', 'gold_top')}, precision {pct('top_hit', 'sys_top')}"
    )
    print(f"top-level reachable (perfect keep/drop): {pct('top_reach', 'gold_top')}")
    print(
        f"sub-words exact span: recall {pct('sub_hit', 'gold_sub')}, precision {pct('sub_hit', 'sys_sub')}"
    )
    print(f"sub-words reachable: {pct('sub_reach', 'gold_sub')}")
    print(f"dict_form on matched spans: {pct('lemma_ok', 'top_hit')}")
    print(f"reading on matched spans:   {pct('reading_ok', 'top_hit')}")
    print(f"match_data (default flags) on matched spans: {pct('flags_ok', 'top_hit')}")
    if verbose:
        print("\ndict_form mismatches (ex, gold, generated):")
        for m in lemma_miss:
            print("  ", m)
        print("reading mismatches (ex, word, gold, generated):")
        for m in reading_miss:
            print("  ", m)


if __name__ == "__main__":
    main(verbose="-q" not in sys.argv)
