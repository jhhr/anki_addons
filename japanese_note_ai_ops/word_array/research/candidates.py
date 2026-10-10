"""Show what the rule stages see for each gold sentence: the words after grouping (with each
compound's short-unit split), the JMdict multi-word candidates, and the words chosen.

    python word_array/research/candidates.py [example numbers...]
"""

import sys

import gold
from _bootstrap import load

generator = load("generator")


def describe_word(tm, w) -> str:
    text = tm.written_form(w.start, w.end)
    if w.subs:
        text += " = " + " + ".join(f"({describe_word(tm, s)})" for s in w.subs)
    return f"{text} [{w.kind}]" if w.kind != "word" else text


def main(nums: list[int]) -> None:
    for num, (sentence, _) in sorted(gold.load().items()):
        if nums and num not in nums:
            continue
        an = generator.analyze(sentence)
        tm, words = an.text_map, an.words
        print(f"\n#{num} {gold.strip_to_plain(tm.raw)}")
        print("  words:", " | ".join(describe_word(tm, w) for w in words if w.kind != "punct"))
        for c in an.candidates:
            parts = " + ".join(tm.written_form(w.start, w.end) for w in words[c.i : c.j])
            refused = "" if generator.is_word_match(c, words) else "  (refused)"
            print(f"  cand: {c.form} = {parts}  pos={sorted(c.pos)}{refused}")
        print("  final:", " | ".join(describe_word(tm, w) for w in an.final if w.kind != "punct"))


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]])
