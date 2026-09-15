"""What one spelling per JMdict entry (generator.canonical_form) changes over the export.

A. dict_form respellings: before (the form as dict_form gives it) -> after, with a sample each.
B. words left as written because their one entry has several kanji choices (よる: 依る/因る),
   the list task 24's re-kanjify takes: kana-written ones first.

    python word_array/research/canonical_forms.py [--out FILE]
"""

import argparse
from collections import Counter

from _bootstrap import ADDON_ROOT, load
from migrate_fit import CORPORA, export_name_lexicon, read_export

generator = load("generator")


def flat(elems, out):
    for e in elems:
        if len(e) > 1:
            out.append((e[2], e[1], e[3]))
            flat(e[5], out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ADDON_ROOT / "output" / "canonical_forms_report.txt"))
    args = ap.parse_args()
    lexicon = export_name_lexicon()
    canonical = generator.canonical_form
    changes, examples, choices, choice_examples = Counter(), {}, Counter(), {}
    rows = changed_rows = failed = 0
    for sentence, _ in read_export(CORPORA["export"], Counter()):
        rows += 1
        try:
            generator.canonical_form = lambda form, reading: form
            before = flat(generator.generate(sentence, lexicon), [])
            generator.canonical_form = canonical
            after = flat(generator.generate(sentence, lexicon), [])
        except Exception:  # survey only
            generator.canonical_form = canonical
            failed += 1
            continue
        if before != after:
            changed_rows += 1
        for (b, pos, reading), (a, _, _) in zip(before, after):
            if a != b:
                key = (b, a, pos)
                changes[key] += 1
                examples.setdefault(key, sentence)
            else:
                several = [s for s in generator.entry_spellings(a, reading) if len(s) > 1]
                if len(generator.entry_spellings(a, reading)) == 1 and several:
                    key = (a, reading, pos, "/".join(several[0]))
                    choices[key] += 1
                    choice_examples.setdefault(key, sentence)
    kana_first = sorted(
        choices.items(), key=lambda kv: (bool(generator.KANJI_RE.search(kv[0][0])), -kv[1])
    )
    with open(args.out, "w", encoding="utf-8") as out:
        out.write(
            f"A. respelled: {len(changes)} distinct, {sum(changes.values())} words in "
            f"{changed_rows} of {rows} sentences ({failed} failed)\n"
        )
        for (b, a, pos), c in changes.most_common():
            out.write(f"{c:5}  {b} -> {a} ({pos})   {examples[(b, a, pos)][:60]}\n")
        out.write(
            f"\nB. kept as written, several kanji choices: {len(choices)} distinct, "
            f"{sum(choices.values())} words\n"
        )
        for (form, reading, pos, spellings), c in kana_first:
            out.write(
                f"{c:5}  {form} [{reading}] ({pos}) -> {spellings}   "
                f"{choice_examples[(form, reading, pos, spellings)][:60]}\n"
            )
    print(args.out)


if __name__ == "__main__":
    main()
