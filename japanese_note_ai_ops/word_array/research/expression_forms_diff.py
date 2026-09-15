"""Words whose dict_form changes when inflected expressions take their dictionary form
(generator.ENTRENCHED_EXPRESSIONS aside), over the export: before = every expression as written.

    python word_array/research/expression_forms_diff.py [--out FILE]
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
    ap.add_argument(
        "--out", default=str(ADDON_ROOT / "output" / "expression_forms_diff_report.txt")
    )
    args = ap.parse_args()
    lexicon = export_name_lexicon()
    entrenched = generator._entrenched
    changes, examples, rows = Counter(), {}, 0
    for sentence, _ in read_export(CORPORA["export"], Counter()):
        try:
            generator._entrenched = lambda form, hits: True
            before = Counter(flat(generator.generate(sentence, lexicon), []))
            generator._entrenched = entrenched
            after = Counter(flat(generator.generate(sentence, lexicon), []))
        except Exception:  # survey only
            generator._entrenched = entrenched
            continue
        if before == after:
            continue
        rows += 1
        key = (
            tuple(sorted((before - after).elements())),
            tuple(sorted((after - before).elements())),
        )
        changes[key] += 1
        examples.setdefault(key, sentence)
    with open(args.out, "w", encoding="utf-8") as out:
        out.write(f"{rows} sentences changed, {len(changes)} distinct changes\n")
        for (gone, new), c in changes.most_common():
            out.write(
                f"{c:5}  -{list(gone)}\n       +{list(new)}\n       {examples[(gone, new)][:70]}\n"
            )
    print(args.out)


if __name__ == "__main__":
    main()
