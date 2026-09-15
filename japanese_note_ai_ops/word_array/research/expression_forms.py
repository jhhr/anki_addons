"""Base words spelled more than one way over the export.

A. expressions matched as written whose deinflected form is another JMdict entry (気を付けて beside
   気を付ける), with a sample sentence each: the list task 17's keep-as-written exceptions are
   sorted from.
B. one JMdict entry (same kebs + rebs) reached by several dict_form spellings (気をつける /
   気を付ける), task 18's survey.

    python word_array/research/expression_forms.py [--out FILE]
"""

import argparse
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load
from migrate_fit import CORPORA, export_name_lexicon, read_export

generator = load("generator")
jmdict = load("jmdict_index")


def real(elems):
    return [e for e in elems if len(e) > 1]


def entry_of(form, reading):
    es = [e for e in jmdict.lookup(form) if reading in e[1]]
    return es[0] if len(es) == 1 else None


def walk(elems, sentence, a_hits, a_examples, b_forms):
    for e in real(elems):
        _, pos, form, reading, _, subs = e
        ent = entry_of(form, reading)
        if ent:
            b_forms[(ent[0], ent[1])][form] += 1
        subs_r = real(subs)
        if pos == "expression" and len(subs_r) >= 2 and subs_r[-1][1] in ("verb", "adjective"):
            deinf = "".join(s[2] for s in subs_r[:-1]) + subs_r[-1][2]
            if deinf != form:
                own = {(k, r) for k, r, _ in jmdict.lookup(form)}
                other = [x for x in jmdict.lookup(deinf) if (x[0], x[1]) not in own]
                if other:
                    key = (form, deinf, ",".join(sorted(other[0][2])))
                    a_hits[key] += 1
                    a_examples.setdefault(key, sentence)
        walk(subs, sentence, a_hits, a_examples, b_forms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ADDON_ROOT / "output" / "expression_forms_report.txt"))
    args = ap.parse_args()
    lexicon = export_name_lexicon()
    a_hits, a_examples, b_forms = Counter(), {}, defaultdict(Counter)
    failed = 0
    for sentence, _ in read_export(CORPORA["export"], Counter()):
        try:
            arr = generator.generate(sentence, lexicon)
        except Exception:  # survey only
            failed += 1
            continue
        walk(arr, sentence, a_hits, a_examples, b_forms)
    with open(args.out, "w", encoding="utf-8") as out:
        out.write(
            f"A. surface expression whose deinflected form is another entry: {len(a_hits)} "
            f"distinct, {sum(a_hits.values())} uses ({failed} rows failed)\n"
        )
        for (form, deinf, pos), c in a_hits.most_common():
            out.write(f"{c:5}  {form} -> {deinf} ({pos})   {a_examples[(form, deinf, pos)][:60]}\n")
        multi = {k: v for k, v in b_forms.items() if len(v) > 1}
        out.write(
            f"\nB. entries reached by >1 dict_form spelling: {len(multi)} of {len(b_forms)} "
            f"entries, {sum(sum(v.values()) for v in multi.values())} uses\n"
        )
        for k, v in sorted(multi.items(), key=lambda kv: -sum(kv[1].values())):
            out.write(f"{sum(v.values()):5}  {dict(v.most_common())}   JMdict kebs {k[0][:4]}\n")
    print(args.out)


if __name__ == "__main__":
    main()
