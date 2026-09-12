"""How much of the old word lists the migrator can fit into a generated array.

The fine-tuning set (`output/extract_words_fine_tuning.jsonl`) is a validated corpus of the old
format: each line's user message ends with the sentence and its assistant message is the word
list extract_words produced for it. The entries there carry no note ids - the training format
drops them - so each distinct (word, reading) is given a synthetic one, which makes every entry
a link the migration has to carry and turns the run into a measurement of exactly what would be
lost on real notes.

    python word_array/research/migrate_fit.py [-q] [-n COUNT] [--reason no_element]

Prints the share of entries carried over, by the step that found the element, and then the
leftovers grouped by category and word, worst first.
"""

import argparse
import json
import sys
import traceback
from collections import Counter
from pathlib import Path

from _bootstrap import ADDON_ROOT, load, load_root

generator = load("generator")
migrate = load("migrate")
html_stripping = load_root("html_stripping")

CORPUS = ADDON_ROOT / "output" / "extract_words_fine_tuning.jsonl"
MARKER = "The sentence to process: "


def read_corpus(path: Path) -> list[tuple[str, dict]]:
    """(sentence, word list dict) for every line that holds both."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        messages = json.loads(line).get("messages", [])
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        assistant = next((m["content"] for m in messages if m["role"] == "assistant"), "")
        if MARKER not in user:
            continue
        # The op strips the <i> context sentences before generating, so this has to as well
        sentence = html_stripping.strip_context_sentences(user.split(MARKER)[-1].strip())
        try:
            word_lists = json.loads(assistant)
        except json.JSONDecodeError:
            continue
        if sentence and isinstance(word_lists, dict):
            out.append((sentence, word_lists))
    return out


def with_note_ids(word_lists: dict) -> dict:
    """The same lists with a synthetic note id on every entry, one per (word, reading) so that
    the same word listed under two categories stays the one link it was."""
    ids: dict[tuple, int] = {}
    out = {}
    for category, values in word_lists.items():
        if not isinstance(values, list):
            continue
        entries = []
        for value in values:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                entries.append(value)
                continue
            word, reading = value[0], value[1]
            note_id = ids.setdefault((word, reading), len(ids) + 1)
            entries.append([word, reading, "", note_id])
        out[category] = entries
    return out


def dump(index: int, sentence: str, arr: list, report, word: str) -> None:
    """The sentence, the words the generator made of it and the leftovers, for one lost word."""
    print(f"\n[{index}] {sentence}")
    for depth, elem in migrate.match_flags.iter_words(arr):
        flag = f" {elem[4]}" if elem[4] else ""
        print(f"  {'  ' * depth}{elem[2]}[{elem[3]}] {elem[1]}{flag}")
    for leftover in report.leftovers:
        mark = "->" if leftover.entry.word == word else "  "
        print(f"  {mark} lost: {leftover}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--quiet", action="store_true", help="the summary only")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--reason", default="", help="list only leftovers of this reason")
    parser.add_argument("--limit", type=int, default=40, help="leftover lines to print")
    # \uXXXX escapes are accepted because this environment's shell mangles Japanese arguments
    parser.add_argument(
        "--dump", default="", help=r"show the arrays where this word is lost (\uXXXX allowed)"
    )
    parser.add_argument("--traceback", action="store_true", help="traceback on generator failure")
    parser.add_argument("--step", default="", help="list what this match step linked, instead")
    args = parser.parse_args()
    if r"\u" in args.dump:
        args.dump = args.dump.encode("ascii", "backslashreplace").decode("unicode_escape")

    corpus = read_corpus(CORPUS)
    if args.n:
        corpus = corpus[: args.n]
    if not corpus:
        print(f"No sentences read from {CORPUS}")
        return 1

    totals: Counter[str] = Counter()
    lost: Counter[tuple[str, str, str, str]] = Counter()
    linked: Counter[tuple[str, str, str, str, str, str]] = Counter()
    failed = 0
    for index, (sentence, word_lists) in enumerate(corpus):
        try:
            arr = generator.generate(sentence)
        except Exception as e:  # a generator failure is not a migration failure
            failed += 1
            if not args.quiet:
                print(f"[{index}] generate failed: {type(e).__name__}: {e}\n    {sentence}")
            if args.traceback:
                traceback.print_exc()
            continue
        report = migrate.migrate(with_note_ids(word_lists), arr)
        if args.dump and any(lo.entry.word == args.dump for lo in report.leftovers):
            dump(index, sentence, arr, report, args.dump)
        totals["entries"] += report.entries
        totals["linked"] += report.linked
        totals["spread over several occurrences"] += report.spread
        for step, count in report.by_step.items():
            totals[f"step {step}"] += count
        for leftover in report.leftovers:
            totals[f"lost {leftover.reason}"] += 1
            lost[
                (
                    leftover.reason,
                    leftover.entry.category,
                    leftover.entry.word,
                    leftover.entry.reading,
                )
            ] += 1
        for entry, elem in report.linked_by_step.get(args.step, []):
            linked[(entry.category, entry.word, entry.reading, elem[2], elem[3], elem[1])] += 1

    entries = totals["entries"] or 1
    print(f"{len(corpus)} sentences, {failed} the generator refused")
    print(
        f"{totals['entries']} entries, {totals['linked']} carried over"
        f" ({100 * totals['linked'] / entries:.1f}%)"
    )
    print(f"  spread over several occurrences: {totals['spread over several occurrences']}")
    for key in sorted(k for k in totals if k.startswith("step ")):
        print(f"  {key}: {totals[key]}")
    for key in sorted(k for k in totals if k.startswith("lost ")):
        print(f"  {key}: {totals[key]} ({100 * totals[key] / entries:.1f}%)")

    if args.quiet:
        return 0
    if args.step:
        print(f"\nWhat the {args.step} step linked, most first:")
        for (category, word, reading, form, read, pos), count in linked.most_common(args.limit):
            print(f"  {count:4} {category:<14} {word}[{reading}] -> {form}[{read}] {pos}")
        return 0
    print("\nLeftovers, worst first:")
    shown = 0
    for (reason, category, word, reading), count in lost.most_common():
        if args.reason and reason != args.reason:
            continue
        print(f"  {count:4} {reason:<10} {category:<14} {word}[{reading}]")
        shown += 1
        if shown >= args.limit:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
