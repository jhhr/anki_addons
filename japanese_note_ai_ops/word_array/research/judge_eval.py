"""Scoring the word matching judge against the hand-checked export.

`output/extract_words_migration_data_checked.jsonl` holds old word lists checked by hand, so they
stand in for the judge's ground truth (the user's call): a word of the generated array that an
old entry fits was judged worth a note, `match`; a word no entry fits was left out on purpose,
`dontmatch`. Particles and the copula were listed unevenly, so a word of those parts of speech
that no entry fits gets no expectation. Numbers the generator already judged `dontmatch` are
shown to the judge as context and score nothing.

    py -3.10 word_array/research/judge_eval.py build
    python word_array/research/judge_eval.py run [--model MODEL] [-n COUNT] [--workers 8]

`build` writes `output/word_matching_judge_eval.jsonl`, one row per sentence: the sentence, its
array with every word unjudged, and `expected`, a label or null per word in `iter_words` order.
It needs SudachiPy, hence `py -3.10`. `run` sends each row's judge prompt to the model (the op's
own `word_matching_judge_model`, else `extract_words_model`, from the add-on's config) through
the op's request code, caches the responses by model and prompt in
`output/word_matching_judge_eval_results.jsonl` so that a rerun only pays for prompts that
changed, and prints the scores and the words the judge got wrong most often.
"""

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from _bootstrap import ADDON_ROOT

EVAL_SET = ADDON_ROOT / "output" / "word_matching_judge_eval.jsonl"
RESULTS = ADDON_ROOT / "output" / "word_matching_judge_eval_results.jsonl"
NO_EXPECTATION_POS = ("particle", "copula")


def label_array(word_lists: dict, arr: list, migrate, match_flags) -> list:
    """The expected judgement of every word of `arr`, in `iter_words` order: "match" where an
    old entry fits the word, "dontmatch" where none does, None where nothing is expected."""
    elements = [elem for _, elem in match_flags.iter_words(arr)]
    found: set[int] = set()
    entries, _ = migrate.read_word_lists(word_lists)
    for entry in entries:
        if entry.word and entry.reading:
            hits, _ = migrate._find(entry, elements)
            found.update(id(elem) for elem in hits)
    expected = []
    for elem in elements:
        if match_flags.match_state(elem) != match_flags.MatchState.UNJUDGED:
            expected.append(None)
        elif id(elem) in found:
            expected.append(match_flags.MATCH)
        elif elem[1] in NO_EXPECTATION_POS:
            expected.append(None)
        else:
            expected.append(match_flags.DONT_MATCH)
    return expected


def build(args) -> int:
    import migrate_fit

    migrate, match_flags = migrate_fit.migrate, migrate_fit.migrate.match_flags
    invalid: Counter[str] = Counter()
    corpus = migrate_fit.read_export(migrate_fit.CORPORA["checked"], invalid)
    labels: Counter[tuple[str, str]] = Counter()
    rows = []
    for raw_sentence, word_lists in corpus:
        sentence = migrate_fit.html_stripping.strip_context_sentences(raw_sentence)
        if not sentence.strip():
            invalid["no sentence once the context is stripped"] += 1
            continue
        arr = migrate_fit.generator.generate(sentence)
        expected = label_array(word_lists, arr, migrate, match_flags)
        for (_, elem), label in zip(match_flags.iter_words(arr), expected):
            labels[(str(label), elem[1])] += 1
        rows.append({"sentence": sentence, "array": arr, "expected": expected})

    with EVAL_SET.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{EVAL_SET.name}: {len(rows)} sentences")
    for key, count in invalid.most_common():
        print(f"  input problem: {key}: {count}")
    for label in ("match", "dontmatch", "None"):
        by_pos = {pos: n for (lab, pos), n in labels.items() if lab == label}
        top = ", ".join(f"{pos} {n}" for pos, n in Counter(by_pos).most_common(8))
        print(f"  {label}: {sum(by_pos.values())} - {top}")
    return 0


def load_judge():
    """The op module and its match_flags, loaded with Anki stubbed and the add-on's real config
    behind `mw.addonManager.getConfig`, so the requests go out exactly as the op sends them."""
    sys.path.insert(0, str(ADDON_ROOT / "test"))
    from addon_modules import load_ops_module, mw

    config = json.loads((ADDON_ROOT / "config.json").read_text(encoding="utf-8"))
    meta = ADDON_ROOT / "meta.json"
    if meta.exists():
        config.update(json.loads(meta.read_text(encoding="utf-8")).get("config", {}))
    mw.addonManager = SimpleNamespace(getConfig=lambda _name: config)
    judge = load_ops_module("word_matching_judge")
    return judge, load_ops_module("match_flags", subdir="word_array"), config


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


def run(args) -> int:
    if not EVAL_SET.exists():
        print(f"No {EVAL_SET.name}: run `build` first")
        return 1
    judge, match_flags, config = load_judge()
    model = args.model or judge.judge_model(config)
    rows = [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines() if line]
    if args.n:
        rows = rows[: args.n]

    prompts = [match_flags.judge_prompt(row["array"])[0] for row in rows]
    keys = [prompt_key(model, prompt) for prompt in prompts]
    cached = read_results()
    todo = sorted({k: p for k, p in zip(keys, prompts) if k not in cached}.items())
    print(
        f"{len(rows)} sentences, model {model}: {len(todo)} requests, {len(rows) - len(todo)}"
        " cached",
        file=sys.stderr,
    )

    def ask(item):
        key, prompt = item
        return key, judge.get_response(model, prompt, response_schema=judge.RESPONSE_SCHEMA)

    with ThreadPoolExecutor(max_workers=args.workers) as pool, RESULTS.open(
        "a", encoding="utf-8"
    ) as f:
        for done, (key, response) in enumerate(pool.map(ask, todo), 1):
            if response is None:
                continue  # not cached, so a rerun asks again
            row = {"key": key, "model": model, "response": response}
            cached[key] = row
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            if done % 25 == 0:
                print(f"  ...{done}", file=sys.stderr)
    return score(rows, keys, cached, match_flags, args.limit)


def score(rows: list, keys: list, cached: dict, match_flags, limit: int) -> int:
    counts: Counter[str] = Counter()
    wrong_picks: Counter[tuple[str, str, str]] = Counter()
    missed: Counter[tuple[str, str, str]] = Counter()
    by_pos: Counter[tuple[str, str]] = Counter()
    unexpected_picks: Counter[str] = Counter()
    for row, key in zip(rows, keys):
        if key not in cached:
            counts["sentences without a response"] += 1
            continue
        arr = copy.deepcopy(row["array"])
        _, elements = match_flags.judge_prompt(arr)
        try:
            match_flags.apply_judge_response(elements, cached[key]["response"])
        except ValueError:
            counts["sentences with a malformed response"] += 1
            continue
        counts["sentences scored"] += 1
        judged = {id(elem) for elem in elements}
        for (_, elem), expected in zip(match_flags.iter_words(arr), row["expected"]):
            got = elem[4][0] if elem[4] else None
            word = (elem[2], elem[3], elem[1])
            if expected is None:
                # Particles and the copula the judge picked; the prompt tells it not to
                if id(elem) in judged and got == match_flags.DONT_MATCH:
                    unexpected_picks[elem[1]] += 1
                continue
            counts["words"] += 1
            outcome = "right" if got == expected else "wrong"
            by_pos[(elem[1], f"{expected} {outcome}")] += 1
            if got == expected:
                counts[f"{expected} right"] += 1
            elif got == match_flags.DONT_MATCH:
                counts["match picked"] += 1
                wrong_picks[word] += 1
            else:
                counts["dontmatch not picked"] += 1
                missed[word] += 1

    words = counts["words"] or 1
    right_picks = counts["dontmatch right"]
    picks = right_picks + counts["match picked"]
    should_pick = right_picks + counts["dontmatch not picked"]
    print(f"Sentences scored: {counts['sentences scored']}", end="")
    for key in ("sentences without a response", "sentences with a malformed response"):
        if counts[key]:
            print(f", {key}: {counts[key]}", end="")
    print()
    print(
        f"Words with an expectation: {counts['words']}, judged as expected"
        f" {100 * (counts['match right'] + right_picks) / words:.1f}%"
    )
    print(
        f"  picks (dontmatch): {picks}, precision {100 * right_picks / (picks or 1):.1f}%,"
        f" recall {100 * right_picks / (should_pick or 1):.1f}% of {should_pick}"
    )
    print(f"  words wrongly picked (expected match): {counts['match picked']}")
    print(f"  words missed (expected dontmatch): {counts['dontmatch not picked']}")
    if unexpected_picks:
        top = ", ".join(f"{pos} {n}" for pos, n in unexpected_picks.most_common())
        print(f"  picks without an expectation: {top}")

    print("\nBy part of speech: match right/wrong, dontmatch right/wrong")
    for pos in sorted(
        {pos for pos, _ in by_pos}, key=lambda p: -sum(n for (q, _), n in by_pos.items() if q == p)
    ):
        cells = [
            by_pos[(pos, f"{e} {o}")] for e in ("match", "dontmatch") for o in ("right", "wrong")
        ]
        print(f"  {pos:<14} {cells[0]:5}/{cells[1]:<5} {cells[2]:5}/{cells[3]:<5}")
    for title, counter in (
        ("Wrongly picked, most first:", wrong_picks),
        ("Missed, most first:", missed),
    ):
        print(f"\n{title}")
        for (form, reading, pos), count in counter.most_common(limit):
            print(f"  {count:4} {form}[{reading}] {pos}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="write the eval set from the checked export")
    run_parser = sub.add_parser("run", help="ask the judge and score it")
    run_parser.add_argument("--model", default="", help="instead of the configured judge model")
    run_parser.add_argument("-n", type=int, default=0, help="only the first COUNT sentences")
    run_parser.add_argument("--workers", type=int, default=8, help="requests at once")
    run_parser.add_argument("--limit", type=int, default=30, help="wrong words to list")
    args = parser.parse_args()
    return build(args) if args.command == "build" else run(args)


if __name__ == "__main__":
    sys.exit(main())
