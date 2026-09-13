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
It needs SudachiPy, hence `py -3.10`. `run` sends each word's judge prompt to the model (the op's
own `word_matching_judge_model`, else `extract_words_model`, from the add-on's config) through
the op's request code, caches the responses by model and prompt in
`output/word_matching_judge_eval_results.jsonl` so that a rerun only pays for prompts that
changed, and prints the scores and the words the judge got wrong most often.

Words judged by hand in `hand_judge.py` (`output/word_matching_judge_hand_labels.jsonl`) replace
the checked export's label for their word, and a sentence judged by hand outside the checked export
becomes a row expecting only those words, so `run` asks about nothing else in it. Such words are
also scored on a line of their own, HAND.
"""

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import NamedTuple, Optional

import hand_labels
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
    hand_by_sentence: dict[str, list[dict]] = {}
    for label in hand_labels.read_labels():
        hand_by_sentence.setdefault(label["sentence"], []).append(label)
    hand: Counter[str] = Counter()
    labels: Counter[tuple[str, str]] = Counter()
    rows = []

    def add_row(sentence: str, arr: list, expected: list) -> None:
        indices, changed, stale = hand_labels.apply_labels(
            arr, expected, hand_by_sentence.pop(sentence, [])
        )
        hand["applied"] += len(indices)
        hand["changed a checked label"] += changed
        hand["stale, no such word now"] += stale
        for (_, elem), label in zip(match_flags.iter_words(arr), expected):
            labels[(str(label), elem[1])] += 1
        row = {"sentence": sentence, "array": arr, "expected": expected}
        if indices:
            row["hand"] = indices
        rows.append(row)

    for raw_sentence, word_lists in corpus:
        sentence = migrate_fit.html_stripping.strip_context_sentences(raw_sentence)
        if not sentence.strip():
            invalid["no sentence once the context is stripped"] += 1
            continue
        arr = migrate_fit.generator.generate(sentence)
        add_row(sentence, arr, label_array(word_lists, arr, migrate, match_flags))
    checked_rows = len(rows)
    # Sentences judged by hand outside the checked export expect only what was judged
    for sentence in list(hand_by_sentence):
        arr = migrate_fit.generator.generate(sentence)
        add_row(sentence, arr, [None] * len(list(match_flags.iter_words(arr))))
    if hand:
        print(
            f"hand labels: {hand['applied']} applied, {len(rows) - checked_rows} sentences"
            f" beyond the checked export, {hand['changed a checked label']} changed a checked"
            f" label, {hand['stale, no such word now']} stale"
        )

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
    judge = load_ops_module("word_matching_judgev2")
    match_flags = load_ops_module("match_flags", subdir="word_array")
    return judge, match_flags, load_ops_module("judge_v2", subdir="word_array"), config


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


class Judged(NamedTuple):
    """One eval row after the judge: its array with the decisions written in, the words decided
    by a request, each such word's reason where the judge gives one, and the rule group of every
    word asked about. `arr` is None when the row has no usable response."""

    row: dict
    arr: Optional[list]
    asked: set[int]
    reasons: dict[int, str]
    groups: dict[int, str]


def scored_asks(row: dict, arr: list, plan, match_flags) -> list:
    """The plan's asks about words with an expectation: a sentence judged only in part by hand
    asks about nothing else."""
    expected = {
        id(elem): label for (_, elem), label in zip(match_flags.iter_words(arr), row["expected"])
    }
    return [ask for ask in plan.asks if expected[id(ask.elem)] is not None]


def row_requests(rows: list, model: str, judge_v2, match_flags) -> list[list[tuple[str, str]]]:
    """(key, prompt) of every request each row needs: one per scored word not auto-judged."""
    out = []
    for row in rows:
        arr = copy.deepcopy(row["array"])
        asks = scored_asks(row, arr, judge_v2.plan_judgements(arr), match_flags)
        out.append([(prompt_key(model, ask.prompt), ask.prompt) for ask in asks])
    return out


def apply_row(row: dict, keys: list[str], cached: dict, judge_v2, match_flags) -> Judged:
    """The row judged; a word whose request has no usable response stays unjudged."""
    arr = copy.deepcopy(row["array"])
    plan = judge_v2.plan_judgements(arr)
    judge_v2.set_auto(plan)
    asked, reasons = set(), {}
    groups = {id(ask.elem): ask.group for ask in plan.asks}
    for ask, key in zip(scored_asks(row, arr, plan, match_flags), keys):
        response = cached.get(key, {}).get("response")
        try:
            judge_v2.apply_word_response(ask.elem, response)
        except ValueError:
            continue
        asked.add(id(ask.elem))
        reasons[id(ask.elem)] = str(response.get(judge_v2.REASON_FIELD, ""))
    return Judged(row, arr, asked, reasons, groups)


def run(args) -> int:
    if not EVAL_SET.exists():
        print(f"No {EVAL_SET.name}: run `build` first")
        return 1
    judge, match_flags, judge_v2, config = load_judge()
    model = args.model or judge.judge_model(config)
    rows = [json.loads(line) for line in EVAL_SET.read_text(encoding="utf-8").splitlines() if line]
    if args.n:
        rows = rows[: args.n]

    requests = row_requests(rows, model, judge_v2, match_flags)
    cached = read_results()
    todo = sorted({k: p for reqs in requests for k, p in reqs if k not in cached}.items())
    if args.score_only:
        todo = []
    total = len({k for reqs in requests for k, _ in reqs})
    print(
        f"{len(rows)} sentences, model {model}: {len(todo)} requests,"
        f" {total - len(todo)} cached",
        file=sys.stderr,
    )

    def ask(item):
        key, prompt = item
        return key, judge.get_response(model, prompt, response_schema=judge_v2.RESPONSE_SCHEMA)

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
            if done % 250 == 0:
                print(f"  ...{done}", file=sys.stderr)
    judged = [
        apply_row(row, [k for k, _ in reqs], cached, judge_v2, match_flags)
        for row, reqs in zip(rows, requests)
    ]
    return score(judged, match_flags, judge_v2, args)


def _rates(c: Counter) -> str:
    """Accuracy and dontmatch precision/recall of a Counter keyed "<expected> <right|wrong>"."""
    right_picks = c["dontmatch right"]
    picks = right_picks + c["match wrong"]
    should_pick = right_picks + c["dontmatch wrong"]
    words = sum(c.values())
    accuracy = 100 * (c["match right"] + right_picks) / (words or 1)
    return (
        f"{words:5} words {accuracy:5.1f}% | match {c['match right']:4}/{c['match wrong']:<4}"
        f" dontmatch {right_picks:4}/{c['dontmatch wrong']:<4} | picks P"
        f" {100 * right_picks / (picks or 1):5.1f}% R {100 * right_picks / (should_pick or 1):5.1f}%"
    )


def score(judged: list[Judged], match_flags, judge_v2, args) -> int:
    counts: Counter[str] = Counter()
    wrong_picks: Counter[tuple[str, str, str]] = Counter()
    missed: Counter[tuple[str, str, str]] = Counter()
    by_group: dict[str, Counter] = {}
    by_pos: dict[str, Counter] = {}
    unexpected_picks: Counter[str] = Counter()
    reasons: dict[tuple[str, str, str], list[str]] = {}
    for item in judged:
        if item.arr is None:
            counts["sentences without a usable response"] += 1
            continue
        counts["sentences scored"] += 1
        hand = set(item.row.get("hand", []))
        words = match_flags.iter_words(item.arr)
        for index, ((_, elem), expected) in enumerate(zip(words, item.row["expected"])):
            got = elem[4][0] if elem[4] else None
            word = (elem[2], elem[3], elem[1])
            if expected is None:
                # Particles and the copula the judge picked; the prompt tells it not to
                if id(elem) in item.asked and got == match_flags.DONT_MATCH:
                    unexpected_picks[elem[1]] += 1
                continue
            if elem[1] in judge_v2.AUTO_DONT_MATCH_POS:
                # The judge decides these by rule (the user's call), so they aren't scored
                counts[f"particle/copula expected {expected}, unscored"] += 1
                continue
            if got not in (match_flags.MATCH, match_flags.DONT_MATCH):
                counts["words without a response"] += 1
                continue
            outcome = f"{expected} {'right' if got == expected else 'wrong'}"
            for key, table in ((item.groups[id(elem)], by_group), (elem[1], by_pos)):
                table.setdefault(key, Counter())[outcome] += 1
            by_group.setdefault("ALL", Counter())[outcome] += 1
            if index in hand:
                by_group.setdefault("HAND", Counter())[outcome] += 1
            if got != expected:
                (wrong_picks if got == match_flags.DONT_MATCH else missed)[word] += 1
                if id(elem) in item.reasons:
                    reasons.setdefault((got, word), []).append(item.reasons[id(elem)])

    print(f"Sentences scored: {counts['sentences scored']}", end="")
    for key in (
        "sentences without a usable response",
        "words without a response",
        "particle/copula expected match, unscored",
        "particle/copula expected dontmatch, unscored",
    ):
        if counts[key]:
            print(f", {key}: {counts[key]}", end="")
    print()
    print("  words acc | match right/wrong, dontmatch right/wrong | dontmatch picks")
    print(f"  {'ALL':<14} {_rates(by_group.pop('ALL', Counter()))}")
    if "HAND" in by_group:
        print(f"  {'HAND':<14} {_rates(by_group.pop('HAND'))}")
    if unexpected_picks:
        top = ", ".join(f"{pos} {n}" for pos, n in unexpected_picks.most_common())
        print(f"  picks without an expectation: {top}")
    for title, table in (("By group", by_group), ("By part of speech", by_pos)):
        print(f"\n{title}:")
        for key, c in sorted(table.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {key:<14} {_rates(c)}")
    for title, counter, got in (
        ("Wrongly picked (expected match), most first:", wrong_picks, match_flags.DONT_MATCH),
        ("Missed (expected dontmatch), most first:", missed, match_flags.MATCH),
    ):
        print(f"\n{title}")
        for word, count in counter.most_common(args.limit):
            form, reading, pos = word
            print(f"  {count:4} {form}[{reading}] {pos}")
            for reason in reasons.get((got, word), [])[: args.reasons]:
                print(f"         - {reason}")
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
    run_parser.add_argument("--reasons", type=int, default=0, help="reasons per wrong word")
    run_parser.add_argument(
        "--score-only", action="store_true", help="send nothing, score the cached responses"
    )
    args = parser.parse_args()
    return build(args) if args.command == "build" else run(args)


if __name__ == "__main__":
    sys.exit(main())
