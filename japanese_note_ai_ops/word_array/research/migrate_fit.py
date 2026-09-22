"""How much of the old word lists the migrator can fit into a generated array.

Three corpora of the old format can be measured:

- `export` (the default): `output/extract_words_migration_data.jsonl`, every sentence of the
  collection with its raw word list field, as the since-removed `make_extract_words_migration_data` wrote it -
  `{"sentence", "word_list", "nids"}` per row (`nids` every note with the sentence, unused here). A field
  that is not valid JSON is counted and skipped; the migration op ran it through `json_repair`,
  which the addon no longer ships, so those few rows are what this can no longer reproduce.
- `checked`: `output/extract_words_migration_data_checked.jsonl`, a hand-checked subset of that.
- `fine_tuning`: `output/extract_words_fine_tuning.jsonl`, a validated corpus whose entries carry
  no note ids - the training format drops them - so each distinct (word, reading) is given a
  synthetic one, which makes every entry a link the migration has to carry.

    python word_array/research/migrate_fit.py [-q] [--corpus export] [-n COUNT] [--reason no_element]

Prints the input problems and crashes, the share of note ids carried over by the step that found
the element, the words dropped and added, and then the leftovers grouped by category and word,
worst first. That report is spent - the migration was run on the collection on 2026-09-16 - but
this module is also the corpus loader every other research script goes through: `CORPORA`,
`read_export` and `export_name_lexicon` are what `hand_judge`, `judge_eval`, `proper_noun_eval`,
`sub_readings`, `okurigana_decomp`, `name_lexicon`, `canonical_forms` and `unbalanced_tags` read
their sentences with.
"""

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from _bootstrap import ADDON_ROOT, load, load_root

generator = load("generator")
migrate = load("research.migrate")
html_stripping = load_root("html_stripping")

CORPORA = {
    "export": ADDON_ROOT / "output" / "extract_words_migration_data.jsonl",
    "checked": ADDON_ROOT / "output" / "extract_words_migration_data_checked.jsonl",
    "fine_tuning": ADDON_ROOT / "output" / "extract_words_fine_tuning.jsonl",
}
MARKER = "The sentence to process: "


def name_lexicon(corpus: list[tuple[str, dict]]) -> dict:
    """The name lexicon of a corpus' sentences. The op builds its lexicon from the whole
    collection's sentences, so give this the export for arrays like the op's."""
    before = time.perf_counter()
    sentences = [html_stripping.strip_context_sentences(s) for s, _ in corpus]
    lexicon = generator.build_name_lexicon(sentences)
    print(f"Name lexicon: {len(lexicon)} names, {time.perf_counter() - before:.0f} s")
    return lexicon


def export_name_lexicon() -> dict:
    """The name lexicon of the export, the collection's sentences."""
    return name_lexicon(read_export(CORPORA["export"], Counter()))


def read_fine_tuning(path: Path, invalid: Counter) -> list[tuple[str, dict]]:
    """(sentence, word list dict) for every line that holds both."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        messages = json.loads(line).get("messages", [])
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        assistant = next((m["content"] for m in messages if m["role"] == "assistant"), "")
        if MARKER not in user:
            invalid["row without a sentence"] += 1
            continue
        try:
            word_lists = json.loads(assistant)
        except json.JSONDecodeError:
            invalid["unreadable word list"] += 1
            continue
        if isinstance(word_lists, dict):
            out.append((user.split(MARKER)[-1].strip(), with_note_ids(word_lists)))
        else:
            invalid["word list not a dict"] += 1
    return out


def decode_word_list(text: str, invalid: Counter):
    """The word list field as JSON, or None, counted, when it is not valid JSON."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        invalid["json unreadable (skipped)"] += 1
        return None


def read_export(path: Path, invalid: Counter) -> list[tuple[str, dict]]:
    """(sentence, word list dict) for every row the migration op would migrate."""
    out: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid["unreadable row"] += 1
            continue
        sentence, text = row.get("sentence") or "", (row.get("word_list") or "").strip()
        if not text:
            # The op generates an array for such a note and carries nothing over
            invalid["empty word list"] += 1
            out.append((sentence, {}))
            continue
        word_lists = decode_word_list(text, invalid)
        if word_lists is None:
            continue
        if not isinstance(word_lists, dict):
            # migrate.migrate would crash on it; the op does not guard against it either
            invalid[f"word list a {type(word_lists).__name__}, not a dict"] += 1
            continue
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


def count_malformed(word_lists: dict, invalid: Counter) -> None:
    """The entries and categories `migrate.read_word_lists` passes over or cannot use."""
    for category, values in word_lists.items():
        if not isinstance(values, list):
            invalid["category not a list"] += 1
            continue
        for value in values:
            entry = migrate.read_entry(str(category), value)
            if entry is None:
                invalid["entry holding nothing"] += 1
            elif not isinstance(value, list):
                invalid[f"entry a bare {type(value).__name__}"] += 1
            elif not entry.word or not entry.reading:
                invalid["entry without a word or reading"] += 1


def dump(index: int, sentence: str, arr: list, report, word: str) -> None:
    """The sentence, the words the generator made of it and the leftovers, for one lost word."""
    print(f"\n[{index}] {sentence}")
    for depth, elem in migrate.match_flags.iter_words(arr):
        flag = f" {elem[4]}" if elem[4] else ""
        print(f"  {'  ' * depth}{elem[2]}[{elem[3]}] {elem[1]}{flag}")
    for leftover in report.leftovers:
        mark = "->" if leftover.entry.word == word else "  "
        print(f"  {mark} lost: {leftover}")


def print_top(title: str, counter: Counter, limit: int) -> None:
    print(f"\n{title}")
    for key, count in counter.most_common(limit):
        print(f"  {count:5} {'  '.join(key) if isinstance(key, tuple) else key}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-q", "--quiet", action="store_true", help="the summary only")
    parser.add_argument("--corpus", choices=CORPORA, default="export")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT sentences")
    parser.add_argument("--reason", default="", help="list only leftovers of this reason")
    parser.add_argument("--limit", type=int, default=40, help="leftover lines to print")
    # \uXXXX escapes are accepted because this environment's shell mangles Japanese arguments
    parser.add_argument(
        "--dump", default="", help=r"show the arrays where this word is lost (\uXXXX allowed)"
    )
    parser.add_argument("--traceback", action="store_true", help="traceback on a crash")
    parser.add_argument("--step", default="", help="list what this match step linked, instead")
    parser.add_argument(
        "--no-names", action="store_true", help="no name lexicon (built from the corpus otherwise)"
    )
    args = parser.parse_args()
    if r"\u" in args.dump:
        args.dump = args.dump.encode("ascii", "backslashreplace").decode("unicode_escape")

    path = CORPORA[args.corpus]
    invalid: Counter[str] = Counter()
    reader = read_fine_tuning if args.corpus == "fine_tuning" else read_export
    corpus = reader(path, invalid)
    if args.n:
        corpus = corpus[: args.n]
    if not corpus:
        print(f"No sentences read from {path}")
        return 1

    lexicon = {} if args.no_names else name_lexicon(corpus)

    totals: Counter[str] = Counter()
    lost: Counter[tuple[str, str, str, str]] = Counter()
    linked: Counter[tuple[str, str, str, str, str, str]] = Counter()
    dropped: Counter[tuple[str, str, str]] = Counter()
    added: Counter[tuple[str, str, str]] = Counter()
    added_pos: Counter[str] = Counter()
    crashes: Counter[str] = Counter()
    slowest: list[tuple[float, int, str]] = []
    started = time.perf_counter()
    for index, (raw_sentence, word_lists) in enumerate(corpus):
        if index and index % 2000 == 0:
            print(f"  ...{index}", file=sys.stderr)
        # The op strips the <i> context sentences before generating, so this has to as well
        sentence = html_stripping.strip_context_sentences(raw_sentence)
        if not sentence.strip():
            invalid["no sentence once the context is stripped"] += 1
            continue
        count_malformed(word_lists, invalid)
        before = time.perf_counter()
        try:
            arr = generator.generate(sentence, lexicon)
        except Exception as e:
            crashes[f"generate: {type(e).__name__}: {e}"[:160]] += 1
            if not args.quiet:
                print(f"[{index}] generate failed: {type(e).__name__}: {e}\n    {sentence}")
            if args.traceback:
                traceback.print_exc()
            continue
        slowest = sorted(slowest + [(time.perf_counter() - before, index, sentence)])[-5:]
        try:
            report = migrate.migrate(word_lists, arr)
        except Exception as e:
            crashes[f"migrate: {type(e).__name__}: {e}"[:160]] += 1
            if not args.quiet:
                print(f"[{index}] migrate failed: {type(e).__name__}: {e}\n    {sentence}")
            if args.traceback:
                traceback.print_exc()
            continue
        totals["sentences migrated"] += 1

        # Dropped and new words, links or not: which old entries find no element at all, and
        # which elements no old entry finds
        elements = [elem for _, elem in migrate.match_flags.iter_words(arr)]
        found: set[int] = set()
        entries, _ = migrate.read_word_lists(word_lists)
        for entry in entries:
            if not entry.word or not entry.reading:
                continue
            hits, _ = migrate._find(entry, elements)
            found.update(id(elem) for elem in hits)
            if not hits:
                totals["old words dropped"] += 1
                if entry.note_id:
                    totals["old words dropped, with a note id"] += 1
                dropped[(entry.category, entry.word, entry.reading)] += 1
        totals["old words"] += len(entries)
        totals["array words"] += len(elements)
        for elem in elements:
            if id(elem) not in found:
                totals["array words no old entry finds"] += 1
                added_pos[elem[1]] += 1
                added[(elem[1], elem[2], elem[3])] += 1

        if args.dump and any(lo.entry.word == args.dump for lo in report.leftovers):
            dump(index, sentence, arr, report, args.dump)
        totals["entries"] += report.entries
        totals["without note id"] += report.without_note_id
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
    elapsed = time.perf_counter() - started

    with_ids = (totals["entries"] - totals["without note id"]) or 1
    print(f"{path.name}: {len(corpus)} sentences, {totals['sentences migrated']} migrated")
    print(f"  {elapsed:.0f} s, {1000 * elapsed / len(corpus):.0f} ms a sentence")
    print(f"Input problems: {sum(invalid.values()) or 'none'}")
    for key, count in invalid.most_common():
        print(f"  {key}: {count}")
    print(f"Crashes: {sum(crashes.values()) or 'none'}")
    for key, count in crashes.most_common():
        print(f"  {count:5} {key}")
    print(
        f"{totals['entries']} entries, {totals['entries'] - totals['without note id']} with a"
        f" note id, {totals['linked']} carried over ({100 * totals['linked'] / with_ids:.1f}%)"
    )
    print(f"  spread over several occurrences: {totals['spread over several occurrences']}")
    for key in sorted(k for k in totals if k.startswith("step ")):
        print(f"  {key}: {totals[key]}")
    for key in sorted(k for k in totals if k.startswith("lost ")):
        print(f"  {key}: {totals[key]} ({100 * totals[key] / with_ids:.1f}%)")
    old, new = totals["old words"] or 1, totals["array words"] or 1
    print(
        f"Old words {totals['old words']}: {totals['old words dropped']} fit no array word"
        f" ({100 * totals['old words dropped'] / old:.1f}%),"
        f" {totals['old words dropped, with a note id']} of them with a note id"
    )
    print(
        f"Array words {totals['array words']}: {totals['array words no old entry finds']} new"
        f" ({100 * totals['array words no old entry finds'] / new:.1f}%) -"
        + ", ".join(f" {pos} {n}" for pos, n in added_pos.most_common(8))
    )
    print("Slowest sentences:")
    for seconds, index, sentence in reversed(slowest):
        print(f"  {seconds:.2f} s [{index}] {sentence[:80]}")

    if args.quiet:
        return 0
    if args.step:
        print(f"\nWhat the {args.step} step linked, most first:")
        for (category, word, reading, form, read, pos), count in linked.most_common(args.limit):
            print(f"  {count:4} {category:<14} {word}[{reading}] -> {form}[{read}] {pos}")
        return 0
    print_top("Old words fitting no array word, most first:", dropped, args.limit)
    print_top("New array words, most first:", added, args.limit)
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
