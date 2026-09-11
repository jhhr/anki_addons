"""Write generated_examples.md: the generated array for every gold sentence with its differences
from the gold, then the kanjify_sentence prompt examples (no gold) as unseen text. Committed so
that changes in the generator's behavior show up as diffs.

    python word_array/research/write_generated.py
"""

import json
from pathlib import Path

import gold
from _bootstrap import load
from evaluate import spans
from validate import kanjify_sentences

generator = load("generator")

OUT = Path(__file__).resolve().parent / "generated_examples.md"


def fmt(arr: list, indent: int = 2) -> str:
    pad = " " * indent
    lines = []
    for e in arr:
        if len(e) >= 6 and e[5]:
            head = json.dumps(e[:5], ensure_ascii=False)[1:-1]
            lines.append(
                f"{pad}[\n{pad}  {head},\n{pad}  [\n{fmt(e[5], indent + 4)}\n{pad}  ]\n{pad}]"
            )
        else:
            lines.append(pad + json.dumps(e, ensure_ascii=False))
    return ",\n".join(lines)


def open_decisions(an) -> list[str]:
    refused = [c.form for c in an.candidates if not generator.is_word_match(c, an.words)]
    return [f"- JMdict matches refused by is_word_match: {refused}"] if refused else []


def main() -> None:
    parts = [
        "# Generated word arrays\n",
        "Regenerate with `python word_array/research/write_generated.py`.\n",
        "## Gold examples\n",
    ]
    for num, (sentence, gold_arr) in sorted(gold.load().items()):
        an = generator.analyze(sentence)
        plain = gold.strip_to_plain(an.text_map.raw)
        parts.append(
            f"```\nExample sentence {num}: {sentence}\n```\n\n```json\n[\n{fmt(an.array)}\n]\n```\n"
        )
        g = {(s, e): el for d, s, e, el in spans(gold_arr) if d == 0}
        s_ = {(s, e): el for d, s, e, el in spans(an.array) if d == 0}
        notes = []
        miss, extra = sorted(g.keys() - s_.keys()), sorted(s_.keys() - g.keys())
        if miss or extra:
            notes.append(
                f"- gold words {[plain[a:b] for a, b in miss]},"
                f" generated {[plain[a:b] for a, b in extra]}"
            )
        for k in sorted(g.keys() & s_.keys()):
            if g[k][2:4] != s_[k][2:4]:
                notes.append(
                    f"- {plain[k[0]:k[1]]}: gold {g[k][2]} / {g[k][3]},"
                    f" generated {s_[k][2]} / {s_[k][3]}"
                )
        notes += open_decisions(an)
        if notes:
            parts.append("Differences and open decisions:\n\n" + "\n".join(notes) + "\n")

    parts.append("## Unseen sentences (kanjify_sentence prompt examples, no gold)\n")
    for i, sentence in enumerate(kanjify_sentences(), 1):
        an = generator.analyze(sentence)
        parts.append(
            f"```\nKanjified example {i}: {sentence}\n```\n\n```json\n[\n{fmt(an.array)}\n]\n```\n"
        )
        notes = open_decisions(an)
        if notes:
            parts.append("Open decisions:\n\n" + "\n".join(notes) + "\n")

    OUT.write_text("\n".join(parts), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
