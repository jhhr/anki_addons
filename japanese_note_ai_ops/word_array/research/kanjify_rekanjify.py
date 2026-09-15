"""Re-kanjify proposals for the kanjify survey's left-kana and meaning words (task 23b).

`kanjify_survey.py` lists words the collection kanjifies in some sentences and leaves kana in
others (left-kana), or kanjifies with different kanji (meaning). Instead of a prompt per word,
kanjify_sentence's own prompt runs again on each migration-export sentence holding such a word
(its `<k>` spans back in kana, as the op's input was), and only the answer's changes on those
words are kept:

  left-kana  a kana use of the word the answer kanjifies -> that piece of the answer in `<k>`
  meaning    a use kanjified with kanji other than the word's most common ones, where the answer
             uses other kanji than the note -> the answer's groups in place of the note's

The answer is placed on the note's text by the kana both read (`kanjify_eval.score_row`'s
alignment). A word whose answer piece doesn't cover exactly its kana, or runs across a tag other
than `<k>`, is "unplaced" and left. Answers failing the op's reverse check are dropped. Policy-
kana uses (て-helpers, である...) are never targets.

Fix rows go to `output/kanjify_rekanjify_fixes.jsonl` in `kanjify_fix.py --fixes` format, merged
with the survey's grammar fixes (`kanjify_survey_fixes.jsonl`): a sentence in both is one row,
the grammar fixes applied first, so this file alone is applied. Every row's `after` passes
`kanjify_note.edit_problem` (reads the same, `<k>` tags pair up). Answers are cached with the
kanjify eval's (`output/kanjify_eval_results.jsonl`, by model and prompt).

    py -3.10 word_array/research/kanjify_rekanjify.py [--model M] [-n COUNT] [--workers 16]
        [--cached-only]

Report `output/kanjify_rekanjify_report.txt`: per word targets / proposed / answer agrees with
the note / unplaced, the kanji proposed, and sample rows.
"""

import argparse
import json
import re
import sys
import threading
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT

import kanjify_audit as audit
import kanjify_eval
import kanjify_fix
import kanjify_note
import note_edits

OUTPUT = ADDON_ROOT / "output"
EXPORT = OUTPUT / "extract_words_migration_data.jsonl"
TASKS = OUTPUT / "kanjify_survey_tasks.jsonl"
GRAMMAR = OUTPUT / "kanjify_survey_fixes.jsonl"
FIXES = OUTPUT / "kanjify_rekanjify_fixes.jsonl"
REPORT = OUTPUT / "kanjify_rekanjify_report.txt"
K_SPAN_RE = re.compile(r"<k>(.*?)</k>", re.S)
CONTEXT_RE = re.compile(r"<i>.*?</i>", re.S)


class Targets(NamedTuple):
    rows: set[int]
    left: set[tuple[str, str]]  # (norm, pos0) of left-kana words
    top: dict[tuple[str, str], set[str]]  # meaning word -> its most common kanji (ties: all)


def read_targets(tasks: list[dict]) -> Targets:
    """The rows to rerun and the words to look for in them: every kana use of a left-kana word,
    every use of a meaning word kanjified with other than its most common kanji."""
    rows, left, top = set(), set(), {}
    for task in tasks:
        key = (task["word"], task["pos"])
        if task["class"] == "left-kana":
            left.add(key)
            rows.update(item["row"] for item in task["items"])
        else:
            most = max(task["kanjified"].values())
            top[key] = {k for k, n in task["kanjified"].items() if n == most}
            rows.update(item["row"] for item in task["items"] if item["kanji"] not in top[key])
    return Targets(rows, left, top)


def source_sentence(field: str) -> str:
    """The op's input the field was kanjified from: every `<k>` span, context too, as kana."""
    return K_SPAN_RE.sub(lambda m: note_edits.as_kana(m[1]) or m[1], field)


def target_class(tok: audit.Tok, targets: Targets) -> Optional[str]:
    if tok.policy:
        return None
    key = audit.word_key(tok)
    if tok.kind == "kana" and key in targets.left:
        return "left-kana"
    if tok.kind == "kanjified" and tok.kanji and tok.kanji not in targets.top.get(key, {tok.kanji}):
        return "meaning"
    return None


class Outcome(NamedTuple):
    cls: str
    word: str  # the note's text of it: よる, 在[あ]る
    result: str  # proposed | agrees | unplaced
    to: str = ""


def _answer_piece(base: kanjify_eval.KanaText, ans: kanjify_eval.KanaText, to_ans: dict,
                  positions: list[int]) -> Optional[tuple[str, str]]:  # fmt: skip
    """The answer's raw text reading exactly these kana of the note, and its kanji; None when
    its segments don't line up with them or a tag other than <k> runs through it."""
    mapped = [to_ans.get(p) for p in positions]
    if None in mapped or mapped != list(range(mapped[0], mapped[0] + len(mapped))):
        return None
    wanted = set(mapped)
    idxs = sorted({ans.owners[q] for q in mapped})
    for idx in idxs:
        if any(q not in wanted for q, owner in enumerate(ans.owners) if owner == idx):
            return None
    segs = ans.tm.segs
    for idx in range(idxs[0], idxs[-1] + 1):
        seg = segs[idx]
        if idx not in idxs and not (seg.kind == "tag" and seg.raw in ("<k>", "</k>")):
            return None
    piece = "".join(segs[i].raw for i in idxs).lstrip(" ")
    if segs[idxs[0]].kind == "furi":
        piece = " " + piece
    kanji = "".join(
        "".join(audit.KANJI_RE.findall(segs[i].base)) for i in idxs if segs[i].kind == "furi"
    )
    return piece, kanji


def propose(
    base: str, answer: str, sentence: str, toks: list[audit.Tok], targets: Targets
) -> tuple[str, list[Outcome]]:
    """`base` with the answer's kanjification of its target words, and what came of each."""
    kb = kanjify_eval.kana_text(base, sentence)
    ka = kanjify_eval.kana_text(answer, sentence)
    to_ans = {}
    for a, b, size in SequenceMatcher(None, kb.kana, ka.kana, autojunk=False).get_matching_blocks():
        to_ans.update((a + i, b + i) for i in range(size))
    to_field = audit._b_free_offsets(base)
    spans = note_edits.k_spans(base)
    context = [(m.start(), m.end()) for m in CONTEXT_RE.finditer(base)]
    edits, outcomes = [], []
    for tok in toks:
        cls = target_class(tok, targets)
        if cls is None:
            continue
        segs = kb.tm.segs_of(tok.morph.start, tok.morph.end)
        if any(s <= to_field[segs[0].raw_start] < e for s, e in context):
            continue
        word = tok.morph.surface if cls == "left-kana" else tok.cut
        idxs = {s.idx for s in segs}
        positions = [p for p, owner in enumerate(kb.owners) if owner in idxs]
        found = None
        if positions and positions == list(range(positions[0], positions[-1] + 1)):
            found = _answer_piece(kb, ka, to_ans, positions)
        if cls == "left-kana":
            # text map offsets skip <b> tags
            start, end = to_field[segs[0].raw_start], to_field[segs[-1].raw_end - 1] + 1
            if base[start:end] != "".join(s.raw for s in segs):
                found = None
        elif tok.field_range is None:
            found = None
        else:
            start, end = tok.field_range
            if "<" in base[start:end]:  # a group outside the span, 天[てん]<k> 婦羅[ぷら]</k>
                found = None
        if found is None:
            outcomes.append(Outcome(cls, word, "unplaced"))
            continue
        piece, kanji = found
        if not kanji or (cls == "meaning" and kanji == tok.kanji):
            outcomes.append(Outcome(cls, word, "agrees"))
            continue
        shown = piece.strip()
        if cls == "left-kana":
            inside = any(s.start < start and end < s.end for s in spans)  # <k> 如何[どう]して</k>
            edits.append((start, end, piece, not inside))
        else:
            edits.append((start, end, tok.lead + shown, False))
        outcomes.append(Outcome(cls, word, "proposed", shown))
    out = base
    for start, end, text, new_span in sorted(edits, reverse=True):
        head, tail = out[:start], out[end:]
        if new_span:
            # next to a span, the word joins it: <k> 其[そ]れ 程[ほど]</k>
            head, opening = (head[:-4], "") if head.endswith("</k>") else (head, "<k>")
            tail, closing = (tail[3:], "") if tail.startswith("<k>") else (tail, "</k>")
            text = opening + text + closing
        out = head + text + tail
    return out, outcomes


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="instead of the config's kanjify_sentence_model")
    parser.add_argument("-n", type=int, default=0, help="only the first COUNT rows to rerun")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--samples", type=int, default=60, help="proposal rows in the report")
    parser.add_argument("--cached-only", action="store_true", help="send nothing")
    args = parser.parse_args()

    import kanjify_survey

    export = kanjify_survey.read_rows(EXPORT)
    targets = read_targets(kanjify_fix.read_jsonl(TASKS))
    grammar = {row["row"]: row for row in kanjify_fix.read_jsonl(GRAMMAR)}
    todo_rows = sorted(targets.rows)[: args.n or None]

    op, config = kanjify_eval.load_op()
    model = args.model or config.get("kanjify_sentence_model", "")
    sentences = {i: source_sentence(export[i][1]) for i in todo_rows}
    keys = {
        i: kanjify_eval.prompt_key(model, op.get_kanjify_sentence_prompt(s))
        for i, s in sentences.items()
    }
    cached = kanjify_eval.read_results()
    todo = sorted(
        {keys[i]: op.get_kanjify_sentence_prompt(sentences[i]) for i in todo_rows
         if keys[i] not in cached}.items()
    )  # fmt: skip
    print(f"{model}: {len(todo_rows)} rows, {len(todo)} requests", file=sys.stderr)
    if todo and not args.cached_only:
        with kanjify_eval.RESULTS.open("a", encoding="utf-8") as f:
            temperature = op.kanjify_temperature(config)
            kanjify_eval.fetch(
                op, model, todo, temperature, args.workers, cached, f, threading.Lock()
            )

    counts: Counter[str] = Counter()
    per_word: dict[tuple[str, str], Counter] = defaultdict(Counter)
    proposed_to: dict[tuple[str, str], Counter] = defaultdict(Counter)
    problems, llm_rows = [], {}
    for i in todo_rows:
        response = cached.get(keys[i], {}).get("response")
        text = response.get(op.KANJIFIED_SENTENCE_RETURN_FIELD) if response else None
        if not isinstance(text, str):
            counts["no answer"] += 1
            continue
        cleaned, reverses = op.clean_kanjified(sentences[i], text)
        if not reverses:
            counts["reverse check failed"] += 1
            continue
        nids, raw = export[i]
        base = grammar[i]["after"] if i in grammar else raw
        try:
            after, outcomes = propose(
                base, cleaned, sentences[i], audit.analyze_row(i, base), targets
            )
        except Exception as e:  # one odd sentence shouldn't stop the run
            problems.append(f"[{i}] {type(e).__name__}: {e}")
            continue
        counts["answered"] += 1
        for o in outcomes:
            per_word[(o.cls, o.word)][o.result] += 1
            counts[o.result] += 1
            if o.to:
                proposed_to[(o.cls, o.word)][o.to] += 1
        fixes = [{"class": o.cls, "word": o.word, "to": o.to} for o in outcomes if o.to]
        if not fixes:
            continue
        problem = kanjify_note.edit_problem(base, after)
        if problem:
            problems.append(f"[{i}] {problem}\n  before {base}\n  after  {after}")
            counts["refused rows"] += 1
            continue
        llm_rows[i] = {"row": i, "nids": nids, "before": raw, "after": after, "fixes": fixes}

    merged = []
    for i in sorted(set(grammar) | set(llm_rows)):
        if i in llm_rows:
            row = llm_rows[i]
            row["fixes"] = grammar.get(i, {}).get("fixes", []) + row["fixes"]
            merged.append(row)
        else:
            merged.append(grammar[i])
    with open(FIXES, "w", encoding="utf-8") as f:
        for row in merged:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = [
        f"{model}: {len(todo_rows)} rows rerun, answered {counts['answered']}, no answer"
        f" {counts['no answer']}, reverse check failed {counts['reverse check failed']}",
        f"target uses: proposed {counts['proposed']}, answer agrees with the note"
        f" {counts['agrees']}, unplaced {counts['unplaced']}",
        f"rows with proposals {len(llm_rows)} ({len(set(llm_rows) & set(grammar))} also grammar"
        f" fixes), refused {counts['refused rows']}, failed {len(problems) - counts['refused rows']}",
        f"fix rows {len(merged)} (grammar {len(grammar)} + re-kanjify) -> {FIXES.name}",
    ]
    lines = ["", "== per word: proposed / agrees / unplaced -> kanji proposed"]
    for (cls, word), c in sorted(per_word.items(), key=lambda kv: -kv[1]["proposed"]):
        to = ", ".join(f"{t} {n}" for t, n in proposed_to[(cls, word)].most_common())
        lines.append(
            f"  {cls:<9} {word}: {c['proposed']} / {c['agrees']} / {c['unplaced']}"
            + (f"  -> {to}" if to else "")
        )
    lines += ["", f"== sample rows (first {args.samples})"]
    lines += kanjify_fix.change_list(list(llm_rows.values())[: args.samples])
    lines += ["", f"== refused or failed rows: {len(problems)}", *problems]
    REPORT.write_text("\n".join(summary + lines) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
