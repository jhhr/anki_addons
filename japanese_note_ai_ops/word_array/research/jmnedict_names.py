"""Would EDRDG's JMnedict (Japanese names: surnames, places, given names...) help find names?

Surveyed on `proper_noun_eval.py`'s sample (export sentences with old proper nouns and as many
without), arrays generated without a name lexicon, a name counted real when the old list's
`proper_nouns` or a model's cached response (`--model`, default gpt-5.6-luna) has it:

- detection: top-level nouns by whether JMdict and JMnedict have them with the word's reading.
  "JMnedict, not JMdict" is what a rule could relabel before the language model; by name type.
- recall ceiling: old proper nouns the generator doesn't make a top-level proper noun, and how
  many JMnedict has as written with the old list's reading.
- guard: the names each model's fix changed that are a single JMdict `n` word (task-46's guard
  blocks them), and which of them a "JMdict n and not JMnedict" guard would let through.

JMnedict.xml.gz (~12 MB) is downloaded into user_files/jmnedict/ when missing, its index pickled
beside it. Sends no requests: models' responses come from the eval's cache only.

    py -3.10 word_array/research/jmnedict_names.py [--model M ...] [-n 300]
"""

import argparse
import copy
import gzip
import pickle
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

import proper_noun_eval as ev
from _bootstrap import ADDON_ROOT

gen = ev.survey.generator
JMNEDICT_URL = "https://www.edrdg.org/pub/Nihongo/JMnedict.xml.gz"
DATA_DIR = ADDON_ROOT / "user_files" / "jmnedict"
JMNEDICT_GZ = DATA_DIR / "JMnedict.xml.gz"
INDEX_PICKLE = DATA_DIR / "jmnedict_index.pkl"
ENTITY_RE = re.compile(r"&([\w.-]+);")
# Words the task asked about: the guard should block the first three, let the rest through
WATCH = ["地球", "日本語", "ピアノ", "ルーク", "真", "根元", "江戸", "八幡", "草薙", "公宗", "高村"]
SAMPLES = 40

# form -> [(hiragana readings, name types)]
NameIndex = dict[str, list[tuple[frozenset[str], frozenset[str]]]]


def build_index() -> NameIndex:
    if not JMNEDICT_GZ.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(JMNEDICT_URL, JMNEDICT_GZ)
    index: NameIndex = {}
    parser = ET.XMLPullParser(events=("end",))
    in_body = False
    with gzip.open(JMNEDICT_GZ, "rt", encoding="utf-8") as f:
        for line in f:
            if not in_body:
                start = line.find("<JMnedict>")
                if start < 0:
                    continue
                line, in_body = line[start:], True
            parser.feed(ENTITY_RE.sub(r"\1", line))
            for _, el in parser.read_events():
                if el.tag != "entry":
                    continue
                kebs = [k.text for k in el.iter("keb") if k.text]
                rebs = [r.text for r in el.iter("reb") if r.text]
                types = frozenset(t.text for t in el.iter("name_type") if t.text)
                readings = frozenset(gen.to_hiragana(r) for r in rebs)
                for form in kebs + rebs:
                    index.setdefault(form, []).append((readings, types))
                el.clear()
    parser.close()
    return index


def load_index() -> NameIndex:
    try:
        return pickle.loads(INDEX_PICKLE.read_bytes())
    except (OSError, pickle.UnpicklingError, EOFError):
        index = build_index()
        INDEX_PICKLE.write_bytes(pickle.dumps(index, protocol=pickle.HIGHEST_PROTOCOL))
        return index


def name_types(index: NameIndex, form: str, reading: str) -> frozenset[str]:
    """The name types of JMnedict's entries spelled `form` and read `reading`."""
    reading = gen.to_hiragana(reading)
    return frozenset(t for rs, ts in index.get(form, []) if reading in rs for t in ts)


def in_jmdict(form: str, reading: str) -> bool:
    return bool(gen._jmdict_codes(form, gen.to_hiragana(reading)))


def types_of(types: frozenset[str]) -> str:
    return "+".join(sorted(types))


def detection(rows, arrays, model_names, index) -> list[str]:
    counts: dict = defaultdict(Counter)
    by_type: dict = defaultdict(Counter)
    examples: dict = defaultdict(Counter)
    for (_, entries), arr, names in zip(rows, arrays, model_names):
        real = {e.word for e in entries} | set(names or [])
        for elem in arr:
            if len(elem) < 2 or elem[1] != "noun":
                continue
            plain = ev.survey.match_flags.plain_text(elem[0]).strip()
            types = name_types(index, elem[2], elem[3])
            jm = in_jmdict(elem[2], elem[3])
            cls = f"{'JMdict' if jm else 'not JMdict'}, {'JMnedict' if types else 'not JMnedict'}"
            is_name = plain in real or elem[2] in real
            counts[cls]["words"] += 1
            counts[cls]["real names"] += is_name
            if types:
                key = types_of(types)
                by_type[cls, key]["words"] += 1
                by_type[cls, key]["real names"] += is_name
            if types and (not jm or is_name):
                mark = "+" if is_name else "-"
                examples[cls][f"{mark} {elem[2]}[{elem[3]}] {types_of(types)}"] += 1
    lines = [
        "== detection: top-level nouns (not proper nouns) by dictionary; real = old list or model"
    ]
    for cls in sorted(counts):
        c = counts[cls]
        lines.append(f"  {cls}: {c['words']} words, {c['real names']} real names")
    for cls in ("not JMdict, JMnedict", "JMdict, JMnedict"):
        lines += ["", f"-- {cls}: by name type (words / real names)"]
        types = sorted(
            ((k, v) for (c, k), v in by_type.items() if c == cls), key=lambda x: -x[1]["words"]
        )
        lines += [f"  {v['words']:5} {v['real names']:5}  {k}" for k, v in types[:25]]
        lines.append(f"-- {cls}: examples (+ real name; JMdict ones only when real)")
        lines += [f"  {n:4} {ex}" for ex, n in examples[cls].most_common(SAMPLES)]
    return lines


def recall_ceiling(rows, arrays, index) -> list[str]:
    counts: dict = defaultdict(Counter)
    examples: dict = defaultdict(Counter)
    for (_, entries), arr in zip(rows, arrays):
        spans = ev.survey.top_spans(arr)
        for entry in entries:
            cls = ev.survey.classify(entry, arr, spans)[0]
            if cls == ev.TOP_PROPER:
                continue
            types = name_types(index, entry.word, entry.reading or "")
            jm = in_jmdict(entry.word, entry.reading or "")
            counts[cls]["names"] += 1
            counts[cls]["JMnedict"] += bool(types)
            counts[cls]["JMnedict, not JMdict"] += bool(types) and not jm
            mark = ("N" if types else "-") + ("J" if jm else "-")
            examples[cls][f"{mark} {entry.word}[{entry.reading}] {types_of(types)}"] += 1
    total = Counter()
    for c in counts.values():
        total.update(c)
    lines = [
        "== recall ceiling: old proper nouns not a top-level proper noun, JMnedict has with the"
        f" old reading: {total['JMnedict']} of {total['names']}"
        f" ({total['JMnedict, not JMdict']} not in JMdict so read); N JMnedict, J JMdict"
    ]
    for cls, c in sorted(counts.items(), key=lambda x: -x[1]["names"]):
        lines += [
            "",
            f"-- {cls}: {c['names']}, JMnedict {c['JMnedict']}, of those not JMdict"
            f" {c['JMnedict, not JMdict']}",
        ]
        lines += [f"  {n:4} {ex}" for ex, n in examples[cls].most_common(SAMPLES // 2)]
    return lines


def guard(model, rows, arrays, names_per_row, llm, index) -> list[str]:
    c: Counter = Counter()
    listed: dict = defaultdict(Counter)
    for (_, entries), arr, names in zip(rows, arrays, names_per_row):
        if names is None:
            continue
        truth = {e.word for e in entries}
        fix = llm.fix_array(copy.deepcopy(arr), names)
        for name in fix.changed:
            tag = "old" if name in truth else "new"
            c[f"changed {tag}"] += 1
            single = [e for e in arr if len(e) > 1 and llm.plain_text(e[0]).strip() == name]
            if not single or single[0][1] == llm.PROPER_NOUN:
                continue
            form, reading = single[0][2], single[0][3]
            if "n" not in gen._jmdict_codes(form, gen.to_hiragana(reading)):
                continue
            types = name_types(index, form, reading)
            verdict = "let through" if types else "blocked"
            c[f"n {tag}"] += 1
            c[f"{verdict} {tag}"] += 1
            listed[f"{verdict} {tag}"][f"{form}[{reading}] {types_of(types)}".strip()] += 1
    lines = [
        f"== guard, {model}: changed old {c['changed old']} / new {c['changed new']};"
        f" JMdict n (task-46 guard blocks) old {c['n old']} / new {c['n new']};"
        f" JMnedict lets through old {c['let through old']} / new {c['let through new']},"
        f" still blocked old {c['blocked old']} / new {c['blocked new']}"
    ]
    for key in ("let through old", "let through new", "blocked old", "blocked new"):
        items = listed[key].most_common()
        lines.append(f"  {key}: " + ", ".join(f"{w}{f' x{n}' if n > 1 else ''}" for w, n in items))
    return lines


def watch(index) -> list[str]:
    lines = ["== watched words: JMdict readings, JMnedict readings (types)"]
    for form in WATCH:
        jm = sorted({gen.to_hiragana(r) for _, rs, _ in gen.jmdict.lookup(form) for r in rs})
        ne = [f"{'/'.join(sorted(rs))} ({types_of(ts)})" for rs, ts in index.get(form, [])]
        lines.append(f"  {form}: JMdict {', '.join(jm) or '-'}; JMnedict {', '.join(ne) or '-'}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", help="default: gpt-5.6-luna")
    parser.add_argument("-n", type=int, default=300, help="sentences with and without names each")
    parser.add_argument("--out", default=str(ADDON_ROOT / "output" / "jmnedict_names_report.txt"))
    args = parser.parse_args()
    models = args.model or ["gpt-5.6-luna"]
    _, llm = ev.load_op()
    index = load_index()
    rows = ev.sample_rows("export", args.n)
    arrays = [gen.generate(sentence) for sentence, _ in rows]
    cached = ev.read_results()

    def names_of(model):
        out = []
        for arr in arrays:
            response = cached.get(ev.prompt_key(model, llm.prompt(arr)), {}).get("response")
            try:
                out.append(llm.names_from_response(response))
            except ValueError:
                out.append(None)
        return out

    per_model = {m: names_of(m) for m in models}
    lines = [f"{len(index)} JMnedict forms; {len(rows)} sentences"]
    lines += [
        f"{m}: cached responses for {sum(n is not None for n in names)} sentences"
        for m, names in per_model.items()
    ]
    lines += [""] + watch(index)
    lines += [""] + detection(rows, arrays, per_model[models[0]], index)
    lines += [""] + recall_ceiling(rows, arrays, index)
    for m, names in per_model.items():
        lines += [""] + guard(m, rows, arrays, names, llm, index)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(ln for ln in lines if not ln.startswith(" ")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
