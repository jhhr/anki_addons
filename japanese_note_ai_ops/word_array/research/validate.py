"""Structural checks on generated arrays for the gold sentences, plus the kanjify_sentence prompt
examples as unseen text:

- the top-level raw_text values reconstruct the sentence
- sub-words make up their parent's text
- wrapping any word in <b> gives balanced html, after use_tag_cleaning.apply_tag_fixes
- sub-word raw_text (with its own share of the furigana) equals the gold's, where spans match

    python word_array/research/validate.py
"""

import re
import time

import gold
from _bootstrap import ADDON_ROOT, load, load_shared
from evaluate import spans

generator = load("generator")
apply_tag_fixes = load_shared("jp_text_processing.word.use_tag_cleaning").apply_tag_fixes

KANJIFY_PY = ADDON_ROOT / "async_api_ops" / "kanjify_sentence.py"
TAG_RE = re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")
VOID_TAGS = {"br", "hr", "img", "wbr"}


def balanced(html: str) -> bool:
    stack = []
    for m in TAG_RE.finditer(html):
        if m.group(2).lower() in VOID_TAGS:
            continue
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


def wrap_variants(arr: list, prefix: str = "", suffix: str = ""):
    """(word raw_text, the sentence with that word wrapped in <b>) for every word at any depth.
    A sub-word is wrapped inside its parent's raw text, which keeps the unsplit furigana."""
    for i, e in enumerate(arr):
        if len(e) <= 1:
            continue
        before = prefix + gold.concat_raw(arr[:i])
        after = gold.concat_raw(arr[i + 1 :]) + suffix
        yield e[0], before + "<b>" + e[0] + "</b>" + after
        if len(e) >= 6 and e[5]:
            yield from wrap_variants(e[5], before, after)


def kanjify_sentences() -> list[str]:
    text = KANJIFY_PY.read_text(encoding="utf-8")
    return re.findall(r"^(?:Kanjified example \d+|Example kanjified \d+): ?(.*)$", text, flags=re.M)


def main() -> None:
    examples = gold.load()
    sentences = [s for s, _ in examples.values()] + kanjify_sentences()
    n_wrap = n_unbalanced = n_unfixable = n_issues = 0
    t0 = time.time()
    for sentence in sentences:
        arr = generator.generate(sentence)
        for issue in gold.validate(sentence, arr):
            n_issues += 1
            print(f"{sentence[:30]}...: {issue}")
        for word_raw, html in wrap_variants(arr):
            n_wrap += 1
            if not balanced(html):
                n_unbalanced += 1
                fixed = apply_tag_fixes(html)
                if not balanced(fixed):
                    n_unfixable += 1
                    print(
                        f"<b> around {word_raw!r} unbalanced even after apply_tag_fixes:\n    {fixed}"
                    )
    elapsed = time.time() - t0

    sub_same = sub_total = 0
    for num, (sentence, gold_arr) in sorted(examples.items()):
        g_sub = {(s, e): el for d, s, e, el in spans(gold_arr) if d > 0}
        s_sub = {(s, e): el for d, s, e, el in spans(generator.generate(sentence)) if d > 0}
        for k in g_sub.keys() & s_sub.keys():
            sub_total += 1
            if g_sub[k][0] == s_sub[k][0]:
                sub_same += 1
            else:
                print(f"ex{num} sub-word raw_text: gold {g_sub[k][0]!r}, generated {s_sub[k][0]!r}")

    print(f"\n{len(sentences)} sentences in {elapsed:.2f}s, {n_issues} structural issues")
    print(
        f"<b> wrapping: {n_wrap} positions, {n_unbalanced} unbalanced as-is,"
        f" {n_unfixable} still unbalanced after apply_tag_fixes"
    )
    print(f"sub-word raw_text equal to gold where spans match: {sub_same}/{sub_total}")


if __name__ == "__main__":
    main()
