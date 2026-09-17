"""Scoring kanjify_sentence's prompt against the hand-fixed kanjified sentences.

Rows are the kanjify audit's (`output/kanjify_sentence_data.jsonl`, else the old fine-tuning
files): the op's input sentence and the checked label. Each row's prompt goes to every model
through the op's own request code, answers cached by model and prompt in
`output/kanjify_eval_results.jsonl` so a rerun only pays for prompts that changed. The answer is
cleaned as the op cleans it (`clean_kanjified`), so what is scored is what the note would get.
Rows whose sentence is one of the prompt's examples are skipped.

    py -3.10 word_array/research/kanjify_eval.py [--model M [--model M2...]] [-n COUNT]
        [--workers 8] [--score-only] [--rows FILE]

Scores per model:
  sentences  exact (whitespace aside), all spans right, reverse check failed (the text changed),
             unparseable answers.
  spans      furigana groups inside `<k>`, placed by the kana they read in the label. Groups of
             label and answer that overlap form one span: *right* with the same kanji (furigana
             cut and `<k>` boundaries aside), *wrong* with other kanji, *missed* when only the
             label kanjified it, *extra* when only the answer did. P = right / (right + wrong +
             extra), R = right / (right + wrong + missed). A label group where the answer's text
             differs can't be placed and is counted as unaligned only.
  classes    policy (a use the kanjify policy writes in kana, as `kanjify_audit.policy_use` reads
             the label), formal noun 事/物/為/様/所, 為る, 成る, 依る/因る, other.

Report `output/kanjify_eval_report_<model>.txt` per model, models side by side on stdout.
"""

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT, load

text_map = load("text_map")

OUTPUT = ADDON_ROOT / "output"
RESULTS = OUTPUT / "kanjify_eval_results.jsonl"
KANJI_RE = re.compile(r"[々〆ヶ一-龯㐀-䶿]")
WS_RE = re.compile(r"\s")
EXAMPLE_RE = re.compile(r"^Example sentence \d+: (.*)$", re.M)
FILENAME_RE = re.compile(r"[^\w.-]")
FORMAL_READINGS = {
    "事": ("こと",),
    "物": ("もの", "もん"),
    "為": ("ため",),
    "様": ("よう",),
    "所": ("ところ", "どころ"),
}
CLASSES = ("policy", "formal noun", "為る", "成る", "依る/因る", "other")
OUTCOMES = ("right", "wrong", "missed", "extra")


class Group(NamedTuple):
    start: int  # kana offsets
    end: int
    kanji: str
    text: str  # as written: 此[こ]れ


class KanaText(NamedTuple):
    kana: str  # every furigana group as its reading, tags and whitespace dropped
    owners: list[int]  # text map segment of each kana char
    groups: list[Group]  # furigana groups inside <k>
    tm: object


def kana_text(text: str, sentence: str) -> KanaText:
    """`text` as the kana it reads, with its `<k>` groups placed in it. A number given furigana
    the source sentence lacks reads as the number, as in the op's reverse check."""
    tm = text_map.build(text)
    kana: list[str] = []
    owners: list[int] = []
    groups = []
    for seg in tm.segs:
        if seg.kind == "tag" or (seg.kind == "char" and seg.raw.isspace()):
            continue
        written = seg.raw.strip()
        if seg.kind == "char":
            chars = seg.raw
        elif seg.base.isdigit() and written not in sentence:
            chars = seg.base
        else:
            chars = seg.reading
        start = len(kana)
        kana.extend(chars)
        owners.extend([seg.idx] * len(chars))
        if seg.kind == "furi" and seg.in_k and chars:
            kanji = "".join(KANJI_RE.findall(seg.base)) or seg.base
            groups.append(Group(start, len(kana), kanji, written))
    return KanaText("".join(kana), owners, groups, tm)


class Comp(NamedTuple):
    outcome: str  # right | wrong | missed | extra
    cls: str
    label: str  # the label's groups as written
    out: str  # the answer's
    context: str


def _reading(group: Group) -> str:
    return group.text[group.text.find("[") + 1 : -1]


def _unrepeated(groups: list[Group]) -> str:
    """The groups' kanji with 々 written out: 抑々 = 抑抑."""
    out = ""
    for ch in "".join(g.kanji for g in groups):
        out += out[-1] if ch == "々" and out else ch
    return out


def span_class(start: int, end: int, groups: list[Group], policy: list[tuple[int, int]]) -> str:
    if any(s < end and start < e for s, e in policy):
        return "policy"
    kanji = "".join(g.kanji for g in groups)
    if "依" in kanji or "因" in kanji:
        return "依る/因る"
    if kanji == "成":
        return "成る"
    if kanji in FORMAL_READINGS and _reading(groups[0]) in FORMAL_READINGS[kanji]:
        return "formal noun"
    if kanji == "為":
        return "為る"
    return "other"


def score_row(
    label: str, output: str, sentence: str, policy: Optional[list[tuple[int, int]]] = None
) -> tuple[list[Comp], int]:
    """The spans of one answer against its label, and how many label groups couldn't be placed
    in the answer's text."""
    lab, out = kana_text(label, sentence), kana_text(output, sentence)
    to_label = {}
    for a, b, size in SequenceMatcher(
        None, out.kana, lab.kana, autojunk=False
    ).get_matching_blocks():
        to_label.update((a + i, b + i) for i in range(size))
    covered = set(to_label.values())
    unaligned = 0
    items = []
    for g in lab.groups:
        if all(i in covered for i in range(g.start, g.end)):
            items.append((g.start, g.end, 0, g))
        else:
            unaligned += 1
    for g in out.groups:
        s, e = to_label.get(g.start), to_label.get(g.end - 1)
        if s is not None and e is not None and e - s == g.end - 1 - g.start:
            items.append((s, e + 1, 1, g))
    spans: list[dict] = []
    for s, e, side, g in sorted(items):
        if not spans or s >= spans[-1]["end"]:
            spans.append({"start": s, "end": e, 0: [], 1: []})
        spans[-1]["end"] = max(spans[-1]["end"], e)
        spans[-1][side].append(g)
    comps = []
    for sp in spans:
        lg, og = sp[0], sp[1]
        if lg and og:
            same = _unrepeated(lg) == _unrepeated(og)
            outcome = "right" if same else "wrong"
        else:
            outcome = "missed" if lg else "extra"
        start, end = sp["start"], sp["end"]
        context = (
            f"{lab.kana[max(0, start - 8):start]}【{lab.kana[start:end]}】{lab.kana[end:end + 8]}"
        )
        comps.append(
            Comp(
                outcome,
                span_class(start, end, lg or og, policy or []),
                "".join(g.text for g in lg),
                "".join(g.text for g in og),
                context,
            )
        )
    return comps, unaligned


def policy_spans(label: str, sentence: str) -> list[tuple[int, int]]:
    """Kana offsets of the label's words the kanjify policy writes in kana (needs SudachiPy)."""
    import kanjify_audit

    lab = kana_text(label, sentence)
    morphs = kanjify_audit.generator.tokenize(lab.tm.natural)
    out = []
    for i, m in enumerate(morphs):
        if kanjify_audit.policy_use(morphs, i) in (None, "check"):
            continue
        segs = {s.idx for s in lab.tm.segs_of(m.start, m.end)}
        at = [p for p, owner in enumerate(lab.owners) if owner in segs]
        if at:
            out.append((min(at), max(at) + 1))
    return out


def load_op():
    """The kanjify op, loaded with Anki stubbed and the add-on's real config behind `mw`."""
    sys.path.insert(0, str(ADDON_ROOT / "test"))
    from addon_modules import load_ops_module, mw

    config = json.loads((ADDON_ROOT / "config.json").read_text(encoding="utf-8"))
    meta = ADDON_ROOT / "meta.json"
    if meta.exists():
        config.update(json.loads(meta.read_text(encoding="utf-8")).get("config", {}))
    mw.addonManager = SimpleNamespace(getConfig=lambda _name: config)
    return load_ops_module("kanjify_sentence"), config


def prompt_key(model: str, prompt: str) -> str:
    return hashlib.sha1(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


def read_results() -> dict[str, dict]:
    if not RESULTS.exists():
        return {}
    out = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["key"]] = row
    return out


def example_sentences(op) -> set[str]:
    return {WS_RE.sub("", s) for s in EXAMPLE_RE.findall(op.get_kanjify_sentence_prompt(""))}


def fetch(op, model: str, todo: list, temperature: float, workers: int, cached: dict, f, lock):
    def ask(item):
        key, prompt = item
        return key, op.get_response(model, prompt, temperature=temperature)

    started, failed = time.monotonic(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, (key, response) in enumerate(pool.map(ask, todo), 1):
            if not isinstance(response, dict):
                failed += 1
                continue  # not cached, so a rerun asks again
            row = {"key": key, "model": model, "response": response}
            with lock:
                cached[key] = row
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
            if done % 100 == 0:
                print(f"  {model} ...{done}", file=sys.stderr)
    minutes = (time.monotonic() - started) / 60
    print(
        f"{model}: {len(todo)} requests ({failed} failed) in {minutes:.1f} min,"
        f" {len(todo) / (minutes or 1):.0f} calls/min at {workers} workers",
        file=sys.stderr,
    )


def _pr(c: Counter) -> tuple[float, float]:
    right = c["right"]
    return (
        100 * right / ((right + c["wrong"] + c["extra"]) or 1),
        100 * right / ((right + c["wrong"] + c["missed"]) or 1),
    )


def _span_line(c: Counter) -> str:
    p, r = _pr(c)
    return (
        f"{sum(c[o] for o in OUTCOMES):5} spans | right {c['right']:4} wrong {c['wrong']:3}"
        f" missed {c['missed']:4} extra {c['extra']:4} | P {p:5.1f}% R {r:5.1f}%"
    )


def evaluate(op, rows: list, keys: list[str], cached: dict, policy: list, limit: int) -> dict:
    """One model's scores and report lines."""
    counts: Counter[str] = Counter()
    spans: Counter[str] = Counter()
    by_class: dict[str, Counter] = defaultdict(Counter)
    words: dict[str, Counter] = defaultdict(Counter)
    examples: dict[tuple, str] = {}
    for i, (row, key) in enumerate(zip(rows, keys)):
        response = cached.get(key, {}).get("response")
        if response is None:
            counts["no answer"] += 1
            continue
        text = response.get(op.KANJIFIED_SENTENCE_RETURN_FIELD)
        if not isinstance(text, str):
            counts["unparseable"] += 1
            continue
        cleaned, reverses = op.clean_kanjified(row.sentence, text)
        counts["scored"] += 1
        counts["reverse check failed"] += not reverses
        counts["exact"] += WS_RE.sub("", cleaned) == WS_RE.sub("", row.label)
        comps, unaligned = score_row(row.label, cleaned, row.sentence, policy[i])
        spans["unaligned"] += unaligned
        counts["all spans right"] += all(c.outcome == "right" for c in comps) and not unaligned
        for c in comps:
            spans[c.outcome] += 1
            by_class[c.cls][c.outcome] += 1
            if c.outcome != "right":
                word = f"{c.label or '-'} -> {c.out or '-'}"
                words[c.outcome][word] += 1
                examples.setdefault((c.outcome, word), f"[{i}] {c.context}")

    scored = counts["scored"] or 1
    lines = [
        f"sentences scored {counts['scored']}, no answer {counts['no answer']},"
        f" unparseable {counts['unparseable']}",
        f"  exact {100 * counts['exact'] / scored:5.1f}%, all spans right"
        f" {100 * counts['all spans right'] / scored:5.1f}%, reverse check failed"
        f" {counts['reverse check failed']}",
        f"  ALL          {_span_line(spans)}, unaligned {spans['unaligned']}",
        "",
        "By class:",
    ]
    lines += [f"  {cls:<12} {_span_line(by_class[cls])}" for cls in CLASSES if by_class[cls]]
    for outcome in ("missed", "extra", "wrong"):
        lines += ["", f"Most {outcome} (label -> answer):"]
        for word, n in words[outcome].most_common(limit):
            lines.append(f"  {n:3} {word}   {examples[(outcome, word)]}")
    return {"counts": counts, "spans": spans, "by_class": by_class, "lines": lines}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", default=[], help="instead of the config's")
    parser.add_argument("--rows", type=Path, help="a kanjify export jsonl")
    parser.add_argument("-n", type=int, default=0, help="only the first COUNT rows")
    parser.add_argument("--workers", type=int, default=8, help="requests at once per model")
    parser.add_argument("--limit", type=int, default=25, help="words to list per outcome")
    parser.add_argument(
        "--score-only", action="store_true", help="send nothing, score the cached answers"
    )
    args = parser.parse_args()

    import kanjify_audit

    op, config = load_op()
    models = args.model or [config.get("kanjify_sentence_model", "")]
    all_rows, source = kanjify_audit.read_rows(args.rows)
    examples = example_sentences(op)
    rows = [r for r in all_rows if WS_RE.sub("", r.sentence) not in examples]
    skipped = len(all_rows) - len(rows)
    rows = rows[: args.n or None]
    print(f"{len(rows)} rows from {source}, {skipped} prompt examples skipped", file=sys.stderr)

    prompts = [op.get_kanjify_sentence_prompt(r.sentence) for r in rows]
    keys = {m: [prompt_key(m, p) for p in prompts] for m in models}
    cached = read_results()
    todo = {
        m: sorted({k: p for k, p in zip(keys[m], prompts) if k not in cached}.items())
        for m in models
    }
    for m in models:
        print(f"{m}: {len(todo[m])} requests, {len(rows) - len(todo[m])} cached", file=sys.stderr)
    if not args.score_only and any(todo.values()):
        temperature = op.kanjify_temperature(config)
        lock = threading.Lock()
        with RESULTS.open("a", encoding="utf-8") as f, ThreadPoolExecutor(len(models)) as pool:
            jobs = [
                pool.submit(fetch, op, m, todo[m], temperature, args.workers, cached, f, lock)
                for m in models
                if todo[m]
            ]
            for job in jobs:
                job.result()

    policy = [policy_spans(r.label, r.sentence) for r in rows]
    results = {m: evaluate(op, rows, keys[m], cached, policy, args.limit) for m in models}
    header = f"{'model':<28} {'scored':>6} {'exact':>6} {'allOK':>6} {'rev✗':>5} {'P':>6} {'R':>6}"
    table = [header]
    for m, res in results.items():
        c, scored = res["counts"], res["counts"]["scored"] or 1
        p, r = _pr(res["spans"])
        table.append(
            f"{m:<28} {c['scored']:6} {100 * c['exact'] / scored:5.1f}%"
            f" {100 * c['all spans right'] / scored:5.1f}% {c['reverse check failed']:5}"
            f" {p:5.1f}% {r:5.1f}%"
        )
    table += ["", "P/R by class:", f"{'model':<28} " + " ".join(f"{c:>13}" for c in CLASSES)]
    for m, res in results.items():
        cells = []
        for cls in CLASSES:
            p, r = _pr(res["by_class"][cls])
            cells.append(f"{p:5.1f}/{r:5.1f}" + ("  " if res["by_class"][cls] else "--"))
        table.append(f"{m:<28} " + " ".join(f"{c:>13}" for c in cells))
    for m, res in results.items():
        path = OUTPUT / f"kanjify_eval_report_{FILENAME_RE.sub('_', m)}.txt"
        head = [f"{m}: {len(rows)} rows from {source}, {skipped} prompt examples skipped", ""]
        path.write_text("\n".join(head + res["lines"]) + "\n", encoding="utf-8")
    print("\n".join(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
