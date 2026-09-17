"""Scoring the proper noun op's models against the old lists' `proper_nouns`.

extract_words was good at proper nouns, so an old list's `proper_nouns` stand in for the truth,
noise included (日本語 and 江戸時代 are listed now and then). A sample of export sentences that
have some and as many that have none is generated without a name lexicon, each model is asked
through the op's own request code, and every model is scored on

- the names it gives against the old list's, exactly as written (precision / recall), and the
  sentences without any where it still gives some;
- the old proper nouns that are a top-level proper noun word before and after `fix_array`
  (the classes of `proper_nouns.py`), and what the fix did: names changed, names not on word
  boundaries, note ids a merge would drop.

Responses are cached by model and prompt in `output/proper_noun_eval_results.jsonl`, so a rerun
only pays for new prompts; a report per model goes to `output/proper_noun_eval_report_<model>.txt`.

    py -3.10 word_array/research/proper_noun_eval.py [--model M ...] [-n 300] [--workers 8]
"""

import argparse
import copy
import hashlib
import json
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import proper_nouns as survey
from _bootstrap import ADDON_ROOT
from migrate_fit import CORPORA, read_export

RESULTS = ADDON_ROOT / "output" / "proper_noun_eval_results.jsonl"
MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5",
]
TOP_PROPER = "top proper noun"


def load_op():
    """The op module and its pure module, loaded with Anki stubbed and the add-on's real config
    behind `mw.addonManager.getConfig`, as `judge_eval.load_judge` does."""
    sys.path.insert(0, str(ADDON_ROOT / "test"))
    from addon_modules import load_ops_module, mw

    config = json.loads((ADDON_ROOT / "config.json").read_text(encoding="utf-8"))
    meta = ADDON_ROOT / "meta.json"
    if meta.exists():
        config.update(json.loads(meta.read_text(encoding="utf-8")).get("config", {}))
    mw.addonManager = SimpleNamespace(getConfig=lambda _name: config)
    return load_ops_module("find_proper_nouns"), load_ops_module("proper_noun_llm", "word_array")


def sample_rows(corpus: str, n: int) -> list[tuple[str, list]]:
    """(sentence, old proper noun entries) of up to `n` sentences with some and `n` without."""
    with_names: list[tuple[str, list]] = []
    without: list[tuple[str, list]] = []
    for raw, word_lists in read_export(CORPORA[corpus], Counter()):
        sentence = survey.html_stripping.strip_context_sentences(raw)
        if not sentence.strip():
            continue
        old, _ = survey.migrate.read_word_lists(word_lists)
        names = [e for e in old if e.category == "proper_nouns" and e.word]
        (with_names if names else without).append((sentence, names))
    rng = random.Random(0)
    return rng.sample(with_names, min(n, len(with_names))) + rng.sample(
        without, min(n, len(without))
    )


def prompt_key(model: str, prompt: str) -> str:
    return hashlib.sha1(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


def read_results() -> dict[str, dict]:
    if not RESULTS.exists():
        return {}
    rows = (json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines() if line)
    return {row["key"]: row for row in rows}


def top_proper(entries: list, arr: list) -> int:
    spans = survey.top_spans(arr)
    return sum(survey.classify(e, arr, spans)[0] == TOP_PROPER for e in entries)


def score(model: str, rows: list, arrays: list, cached: dict, llm) -> list[str]:
    c: Counter = Counter()
    wrong: Counter = Counter()
    missed: Counter = Counter()
    unaligned: Counter = Counter()
    changed: Counter = Counter()
    for (sentence, entries), arr in zip(rows, arrays):
        response = cached.get(prompt_key(model, llm.prompt(arr)), {}).get("response")
        try:
            names = llm.names_from_response(response)
        except ValueError:
            c["no usable response"] += 1
            continue
        truth = {e.word for e in entries}
        c["sentences"] += 1
        c["with names" if truth else "without names"] += 1
        if not truth and names:
            c["names given where none"] += 1
        c["tp"] += len(truth & set(names))
        c["fp"] += len(set(names) - truth)
        c["fn"] += len(truth - set(names))
        wrong.update(set(names) - truth)
        missed.update(truth - set(names))
        fixed = copy.deepcopy(arr)
        fix = llm.fix_array(fixed, names)
        c["old proper nouns"] += len(entries)
        c["top proper before"] += top_proper(entries, arr)
        c["top proper after"] += top_proper(entries, fixed)
        c["changed"] += len(fix.changed)
        c["changed not on old list"] += len(set(fix.changed) - truth)
        c["unaligned"] += len(fix.unaligned)
        c["unlinked"] += len(fix.unlinked)
        unaligned.update(fix.unaligned)
        changed.update(n for n in fix.changed if n not in truth)
    precision = 100 * c["tp"] / ((c["tp"] + c["fp"]) or 1)
    recall = 100 * c["tp"] / ((c["tp"] + c["fn"]) or 1)
    lines = [
        f"== {model}: {c['sentences']} sentences ({c['with names']} with old proper nouns,"
        f" {c['without names']} without), {c['no usable response']} without a usable response",
        f"  names: P {precision:.1f}% R {recall:.1f}% (tp {c['tp']}, fp {c['fp']},"
        f" fn {c['fn']}); names given where the old list has none: {c['names given where none']}"
        f" of {c['without names']} sentences",
        f"  old proper nouns as a top-level proper noun: {c['top proper before']} ->"
        f" {c['top proper after']} of {c['old proper nouns']}",
        f"  fix: {c['changed']} names changed ({c['changed not on old list']} not on the old list),"
        f" {c['unaligned']} not on word boundaries, {c['unlinked']} note ids dropped",
    ]
    for title, counter in (
        ("given, not on the old list", wrong),
        ("on the old list, not given", missed),
        ("changed, not on the old list", changed),
        ("not on word boundaries", unaligned),
    ):
        lines += [f"  -- {title}"] + [f"  {n:4} {w}" for w, n in counter.most_common(40)]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", help=f"default: {', '.join(MODELS)}")
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=300, help="sentences with and without names each")
    parser.add_argument("--workers", type=int, default=8, help="requests at once")
    parser.add_argument("--score-only", action="store_true", help="send nothing")
    args = parser.parse_args()
    op, llm = load_op()
    rows = sample_rows(args.corpus, args.n)
    arrays = [survey.generator.generate(sentence) for sentence, _ in rows]
    cached = read_results()
    for model in args.model or MODELS:
        prompts = {prompt_key(model, llm.prompt(a)): llm.prompt(a) for a in arrays}
        todo = [] if args.score_only else [(k, p) for k, p in prompts.items() if k not in cached]
        print(f"{model}: {len(todo)} requests", file=sys.stderr)

        def ask(item, model=model):
            key, prompt = item
            return key, op.get_response(model, prompt, response_schema=llm.RESPONSE_SCHEMA)

        with ThreadPoolExecutor(max_workers=args.workers) as pool, RESULTS.open(
            "a", encoding="utf-8"
        ) as f:
            for key, response in pool.map(ask, todo):
                if response is None:
                    continue  # not cached, so a rerun asks again
                cached[key] = {"key": key, "model": model, "response": response}
                f.write(json.dumps(cached[key], ensure_ascii=False) + "\n")
                f.flush()
        lines = score(model, rows, arrays, cached, llm)
        out = ADDON_ROOT / "output" / f"proper_noun_eval_report_{model}.txt"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines[:4]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
