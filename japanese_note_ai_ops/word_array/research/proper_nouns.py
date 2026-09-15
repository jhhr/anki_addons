"""How the generator treats names: every old list's `proper_nouns` entry against the array made of
its sentence - one top-level word labelled proper noun, labelled something else (山田 a noun), only
a sub-word, or cut into several top-level words (ひま + りん) - and the other way round, the
generator's proper nouns the old list didn't call one (ツーカー). A name's honorific after it in
the text (ちゃん, 先輩) is counted too, being the context cue a detection could use.

    python word_array/research/proper_nouns.py [--corpus export] [-n COUNT] [--out FILE]
"""

import argparse
from collections import Counter, defaultdict

from _bootstrap import ADDON_ROOT, load, load_root
from migrate_fit import CORPORA, read_export

generator = load("generator")
migrate = load("migrate")
match_flags = load("match_flags")
html_stripping = load_root("html_stripping")

HONORIFICS = (
    "ちゃん",
    "くん",
    "君",
    "さん",
    "様",
    "さま",
    "先輩",
    "先生",
    "氏",
    "殿",
    "たん",
    "っち",
    "りん",
    "たそ",
    "嬢",
)
SAMPLES = 12


def script(word: str) -> str:
    if generator.KANJI_RE.search(word):
        return "kanji" if not migrate.KANA_RE.search(word) else "mixed"
    if all("ァ" <= c <= "ヶ" or c == "ー" for c in word):
        return "katakana"
    return "hiragana" if migrate.KANA_RE.search(word) else "other"


def top_spans(arr: list) -> list[tuple[int, int, list]]:
    """(start, end, element) of the top-level elements, in plain text offsets."""
    out, pos = [], 0
    for elem in arr:
        plain = match_flags.plain_text(elem[0])
        out.append((pos, pos + len(plain), elem))
        pos += len(plain)
    return out


def honorific_after(spans: list, end: int) -> str:
    text = "".join(match_flags.plain_text(e[0]) for _, _, e in spans)[end:]
    return next((h for h in HONORIFICS if text.startswith(h)), "")


def classify(entry, arr: list, spans: list) -> tuple[str, str, list]:
    """The class of an old proper noun, a short description of the words it became, and the
    top-level elements it became (none when it is only a sub-word or not in the text)."""
    hits, _ = migrate._find(entry, _all(arr))
    top = [e for _, _, e in spans if len(e) > 1]
    top_hits = [e for e in hits if any(e is t for t in top)]
    if top_hits:
        labels = sorted({e[1] for e in top_hits})
        cls = "top proper noun" if "proper noun" in labels else f"top {labels[0]}"
        return cls, "", top_hits
    if hits:
        return f"nested {hits[0][1]}", "", []
    plain = "".join(match_flags.plain_text(e[0]) for _, _, e in spans)
    at = plain.find(entry.word)
    if at < 0:
        return "not in text", "", []
    end = at + len(entry.word)
    pieces = [e for s, t, e in spans if s < end and t > at and len(e) > 1]
    desc = " + ".join(f"{e[2]}({e[1]})" for e in pieces)
    if len(pieces) == 1:
        s, t, _ = next(x for x in spans if x[2] is pieces[0])
        cls = "inside a longer word" if (s, t) != (at, end) else "top, form differs"
        return cls, desc, pieces
    return "cut", desc, pieces


def _all(arr: list) -> list:
    return [e for _, e in match_flags.iter_words(arr)]


def oov_offsets(natural: str) -> set[int]:
    return {
        i
        for m in generator._tokenizer().tokenize(natural, generator.SplitMode.C)
        if m.is_oov()
        for i in range(m.begin(), m.end())
    }


def mechanism(w, oov: set[int]) -> str:
    """What made a word what it is: Sudachi's part of speech (固有名詞's subtype too), a JMdict
    match or furigana merge that decides the label, an out-of-vocabulary token."""
    pos = w.head.pos
    tags = [pos[1] if pos[0] == "名詞" else pos[0]]
    if pos[:2] == ("名詞", "固有名詞"):
        tags[0] += "/" + pos[2]
    if w.kind == "expression" or w.jm_pos:
        tags.append("jmdict")
    if w.kind == "merged":
        tags.append("merged")
    if any(i in oov for i in range(w.start, w.end)):
        tags.append("oov")
    return " ".join(tags)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "proper_nouns_report.txt"))
    args = parser.parse_args()
    classes: Counter = Counter()
    by_script: dict = defaultdict(Counter)
    honorifics: Counter = Counter()
    examples: dict = defaultdict(Counter)
    mechanisms: dict = defaultdict(Counter)
    cut_shapes: Counter = Counter()
    extra: Counter = Counter()
    entries = 0
    for raw, word_lists in read_export(CORPORA[args.corpus], Counter())[: args.n or None]:
        sentence = html_stripping.strip_context_sentences(raw)
        if not sentence.strip():
            continue
        analysis = generator.analyze(sentence)
        arr = analysis.array
        spans = top_spans(arr)
        top_elems = [e for e in arr if len(e) > 1]
        top_words = [w for w in analysis.final if w.kind != "punct"]
        word_of = {id(e): w for e, w in zip(top_elems, top_words)}
        oov = oov_offsets(analysis.text_map.natural)
        old, _ = migrate.read_word_lists(word_lists)
        named = {e.word for e in old if e.category == "proper_nouns"}
        for entry in old:
            if entry.category != "proper_nouns" or not entry.word:
                continue
            entries += 1
            cls, desc, elems = classify(entry, arr, spans)
            classes[cls] += 1
            if elems:
                how = " + ".join(mechanism(word_of[id(e)], oov) for e in elems if id(e) in word_of)
                mechanisms[cls][how] += 1
            by_script[cls][script(entry.word)] += 1
            examples[cls][f"{entry.word}[{entry.reading}] {desc}".strip()] += 1
            if cls == "cut":
                cut_shapes[" + ".join(p.split("(")[1][:-1] for p in desc.split(" + "))] += 1
            plain = "".join(match_flags.plain_text(e[0]) for _, _, e in spans)
            at = plain.find(entry.word)
            if at >= 0:
                h = honorific_after(spans, at + len(entry.word))
                honorifics[h or "-"] += 1
        for elem in _all(arr):
            if elem[1] == "proper noun" and elem[2] not in named:
                cats = sorted({e.category for e in old if e.word == elem[2]}) or ["unlisted"]
                sudachi = generator._sudachi_reading(elem[2])
                differs = f" sudachi {sudachi}" if sudachi != elem[3] else ""
                extra[f"{elem[2]}[{elem[3]}] ({','.join(cats)}){differs}"] += 1
    lines = [f"{entries} old proper_nouns entries"]
    for cls, n in classes.most_common():
        scripts = ", ".join(f"{s} {c}" for s, c in by_script[cls].most_common())
        lines += ["", f"== {cls}: {n} ({100 * n / entries:.1f}%) [{scripts}]"]
        if mechanisms[cls]:
            lines.append("  by what made it so:")
            lines += [f"  {c:6} {how}" for how, c in mechanisms[cls].most_common(SAMPLES)]
        lines += [f"  {c:4} {ex}" for ex, c in examples[cls].most_common(SAMPLES)]
    lines += ["", "== cut into (label sequence)"]
    lines += [f"  {c:4} {shape}" for shape, c in cut_shapes.most_common(20)]
    lines += ["", "== honorific right after the name"]
    lines += [f"  {c:4} {h}" for h, c in honorifics.most_common()]
    total_extra = sum(extra.values())
    lines += ["", f"== generator proper nouns the old list didn't call one: {total_extra}"]
    lines += [f"  {c:4} {w}" for w, c in extra.most_common(40)]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:1] + [ln for ln in lines if ln.startswith("== ")]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
