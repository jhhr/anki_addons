"""The name lexicon `names.build_lexicon` makes of a corpus, checked against the old word lists:
how many of its names an old list called a proper noun (precision, by anchor), and how many of the
old proper nouns Sudachi doesn't tag as one 固有名詞 it finds (recall where it is needed).

    python word_array/research/name_lexicon.py [--corpus export] [-n COUNT] [--out FILE]
"""

import argparse
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
names = load("names")
migrate = load("migrate")
jmdict = load("jmdict_index")
html_stripping = load_root("html_stripping")


def one_proper_noun(word: str) -> bool:
    morphs = generator.tokenize(word)
    return len(morphs) == 1 and morphs[0].pos[:2] == ("名詞", "固有名詞")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "name_lexicon_report.txt"))
    args = parser.parse_args()
    corpus = []
    categories: dict = defaultdict(set)
    old_proper: Counter = Counter()
    for raw, word_lists in read_export(CORPORA[args.corpus], Counter())[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if not sentence.strip():
            continue
        corpus.append(generator.name_corpus_row(sentence))
        for entry in migrate.read_word_lists(word_lists)[0]:
            if entry.word:
                categories[entry.word].add(entry.category)
                if entry.category == "proper_nouns":
                    old_proper[entry.word] += 1
    rejected: dict = {}
    lexicon = names.build_lexicon(corpus, word=names.jmdict_word, rejected=rejected)
    found: Counter = Counter()
    for _, morphs, read in corpus:
        found.update(name for _, _, name in names.find_names(morphs, lexicon, read))

    def verdict(name: str) -> str:
        cats = categories.get(name)
        if not cats:
            return "unlisted"
        return "proper" if "proper_nouns" in cats else ",".join(sorted(cats))

    by_source: dict = defaultdict(Counter)
    for name, entry in lexicon.items():
        v = verdict(name)
        for source in entry.sources:
            by_source[source]["proper" if v == "proper" else "other" if v != "unlisted" else v] += 1
    needed = [w for w in old_proper if not one_proper_noun(w)]
    caught = [w for w in needed if w in lexicon]
    lines = [
        f"{len(corpus)} sentences, lexicon {len(lexicon)} names,"
        f" {sum(found.values())} occurrences found",
        f"old proper nouns Sudachi doesn't tag as one: {len(needed)} distinct,"
        f" lexicon has {len(caught)} ({100 * len(caught) / max(1, len(needed)):.1f}%)",
        "",
        "== by anchor: an old list called it a proper noun / something else / never listed it",
    ]
    for source, c in sorted(by_source.items()):
        lines.append(
            f"  {source:10} proper {c['proper']}, other {c['other']}, unlisted {c['unlisted']}"
        )
    lines += ["", "== lexicon (occurrences, anchored count, sources, old lists, JMdict has it)"]
    for name in sorted(lexicon, key=lambda n: (-found[n], n)):
        entry = lexicon[name]
        jm = " jmdict" if jmdict.lookup(name) else ""
        sources = ",".join(sorted(entry.sources))
        lines.append(f"  {found[name]:5} {entry.count:4} {name}  {sources}  [{verdict(name)}]{jm}")
    lines += ["", "== candidates dropped (why, old lists)"]
    lines += [f"  {name}  {why}  [{verdict(name)}]" for name, why in sorted(rejected.items())]
    lines += ["", "== old proper nouns the lexicon misses (Sudachi doesn't tag them as one)"]
    lines += [
        f"  {old_proper[w]:4} {w}"
        for w in sorted(needed, key=lambda w: -old_proper[w])[:80]
        if w not in lexicon
    ]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:9]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
