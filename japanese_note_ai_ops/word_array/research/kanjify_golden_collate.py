"""Collates the kanjify golden set's results (`agent_queue.py`) into what comes next.

    py -3.10 word_array/research/kanjify_golden_collate.py

Reads `results/<queue>/*.json` and writes:

  decisions.jsonl           the step 1 word decisions, as step 2's prompts read them
  collated/accepted.jsonl   step 2 rows that pass the check and wait on nothing: the golden set,
                            in `kanjify_eval.py --rows` format (`sentence`, `kanjified`, `nids`)
                            plus `sid`, `spans` (each `<k>` span, the rule or decision behind it
                            and a confidence), `labeller`, `policy_version`, `decisions_version`
                            and `status` "silver" (gold only once the user has reviewed it)
  collated/pending.jsonl    rows that pass the check but leave words for a decision: relabelled
                            once step 1 has decided them
  collated/rejected.jsonl   rows that fail `kanjify_golden.row_problem`, with the reason
  collated/furigana.jsonl   rows whose input the labeller found broken furigana in, with its
                            fixes, and rows whose input writes kana inside a kanji word's group
                            (`kanjify_golden.kana_in_group`): kept out of the set until the note
                            is fixed, since a label of a misread sentence is no reference; a
                            fixed note is a new sentence id
  collated/furigana_fixes.jsonl  the user's furigana fix list, one row per sentence with any
                            problem: the program's (`kanjify_golden.furigana_suspects`: a missing
                            space or kana inside a group, both repaired in `fixed`, a reading
                            that isn't one kana word, a kanji with none) and the labellers'. A
                            missing space alone keeps the row in the set: it changes no reading,
                            and kanjify_eval compares with whitespace dropped, so the label holds
                            for the fixed sentence. `furigana_fix.py` writes it into the notes
  collated/queue.jsonl      the words this round of step 2 handed back that the inventory has
                            no word for, with their sentences; they are added to
                            `handed_back.jsonl`, which keeps every round's, and
                            `kanjify_golden_render.py words` renders from that
  collated/questions.md     the policy questions agents raised, by word, for the user
  collated/report.md        counts and cost per queue, and the calibration: agreement with the
                            hand-fixed labels (a row the user fixed by hand, labelled again
                            without the agent knowing), between two labellers of one sentence,
                            and between the pilot's three ways

A sentence labelled more than once keeps its first accepted row in accepted.jsonl (batches
before duplicates, by queue name); the others only count in the agreement figures. A sentence a
relabel round labelled again is its latest relabel row alone (`select`).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import kanjify_eval
import kanjify_golden as golden

WS_RE = re.compile(r"\s")
STEP2_QUEUES = ("relabel", "batches", "pilot_batch", "pilot_single")


def read_results(queue: str) -> list[dict]:
    folder = golden.RESULTS / queue
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith(".error.json"):
            continue
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def decisions(results: list[dict]) -> list[dict]:
    words = {w["wid"]: w for w in golden.read_jsonl(golden.INVENTORY / "words.jsonl")}
    queued = {q["wid"]: q for q in golden.read_jsonl(golden.HANDED)}
    out = []
    for r in results:
        s = r.get("structured")
        if not isinstance(s, dict):
            continue
        w = words.get(r["id"]) or queued.get(r["id"]) or {}
        out.append(
            {
                "wid": r["id"],
                "word": w.get("word", s.get("word", "")),
                "kana": w.get("kana", s.get("kana", "")),
                "pos": w.get("pos", ""),
                "uses": s.get("uses", []),
                "not_this_word": s.get("not_this_word", []),
                "questions": s.get("questions", []),
                "summary": s.get("summary", ""),
                "policy_version": r.get("meta", {}).get("policy_version"),
            }
        )
    return out


class Row(dict):
    pass


def step2_rows(queue: str, sentences: dict[str, dict]) -> list[dict]:
    """Every row of a step 2 queue's results, checked: `problem` None for a row that passes."""
    rows = []
    for r in read_results(queue):
        s = r.get("structured")
        meta = r.get("meta", {})
        expected = meta.get("sids", [])
        got = s.get("rows", []) if isinstance(s, dict) else []
        by_sid = {row.get("sid"): row for row in got}
        for sid in expected:
            row = by_sid.get(sid)
            source = sentences[sid]
            base = {
                "sid": sid,
                "sentence": source["sentence"],
                "nids": source["nids"],
                "labeller": f"{queue}/{r['id']}",
                "policy_version": meta.get("policy_version"),
                "decisions_version": meta.get("decisions_version"),
            }
            if row is None:
                rows.append({**base, "kanjified": "", "problem": "the answer has no row for it"})
                continue
            kanjified = row.get("kanjified", "").strip()
            rows.append(
                {
                    **base,
                    "kanjified": kanjified,
                    "spans": row.get("spans", []),
                    "pending": row.get("pending", []),
                    "furigana": row.get("furigana", []),
                    "problem": golden.row_problem(source["sentence"], kanjified),
                }
            )
    return rows


def select(all_rows: dict[str, list[dict]]) -> tuple[dict, list, list, list]:
    """Each checked row to where it goes: (accepted by sid, pending, rejected, broken furigana).
    A sentence a relabel round labelled again (`kanjify_golden_render.py relabel`) is its latest
    relabel row alone, its batch rows and older relabels dropped: that round ran on a newer
    policy or decision table, for exactly the sentences those changed."""
    latest: dict[str, dict] = {}
    for row in sorted(all_rows.get("relabel", []), key=lambda r: r["labeller"], reverse=True):
        latest.setdefault(row["sid"], row)
    accepted: dict[str, dict] = {}
    pending, rejected, furigana = [], [], []
    for queue in STEP2_QUEUES:
        if queue == "relabel":
            rows = list(latest.values())
        else:
            rows = sorted(all_rows.get(queue, []), key=lambda r: r["labeller"])
        for row in rows:
            if queue == "batches" and row["sid"] in latest:
                continue
            # a sentence whose note is to be fixed is held back whatever its label, which
            # labels the broken sentence
            broken = golden.kana_in_group(row.get("sentence", ""))
            if row["problem"] and not broken:
                rejected.append(row)
            elif row["furigana"] or broken:
                furigana.append(row)
            elif row["pending"]:
                pending.append(row)
            elif row["sid"] not in accepted and queue in ("relabel", "batches"):
                accepted[row["sid"]] = {**row, "status": "silver"}
    return accepted, pending, rejected, furigana


def op_rows(sentences: dict[str, dict]) -> list[dict]:
    """The pilot's op answers, cleaned as the op cleans them."""
    op, _ = kanjify_eval.load_op()
    rows = []
    for r in read_results("pilot_op"):
        sid = r.get("meta", {}).get("sids", [None])[0]
        if sid not in sentences:
            continue
        answer = r.get("structured")
        if not isinstance(answer, dict):
            import agent_queue

            answer = agent_queue.terminal_client.last_json_object(r.get("text", "")) or {}
        text = answer.get(op.KANJIFIED_SENTENCE_RETURN_FIELD)
        if not isinstance(text, str):
            rows.append({"sid": sid, "kanjified": "", "problem": "unparseable"})
            continue
        cleaned, _ = op.clean_kanjified(sentences[sid]["sentence"], text)
        rows.append(
            {
                "sid": sid,
                "kanjified": cleaned,
                "problem": golden.row_problem(sentences[sid]["sentence"], cleaned),
            }
        )
    return rows


def fix_list(sentences: dict[str, dict], flagged: list[dict]) -> list[dict]:
    """One row per sentence whose furigana a program or a labeller found wrong."""
    by_sid: dict[str, list[dict]] = defaultdict(list)
    for row in flagged:
        by_sid[row["sid"]] += [{**f, "labeller": row["labeller"]} for f in row["furigana"]]
    out = []
    for sid, s in sentences.items():
        # checked again rather than read from the inventory, which keeps what the check said
        # when the inventory was built: kana inside a group was listed as a missing space there
        program = golden.furigana_suspects(s["sentence"])
        if not program and sid not in by_sid:
            continue
        fixed = golden.fix_groups(s["sentence"])
        out.append(
            {
                "sid": sid,
                "nids": s["nids"],
                "sentence": s["sentence"],
                "program": program,
                "fixed": fixed if fixed != s["sentence"] else None,
                "labeller": by_sid.get(sid, []),
            }
        )
    return out


def merge_handed(old: list[dict], new: list[dict]) -> list[dict]:
    """The handed-back words of every round so far. A round relabelled on a newer policy hands
    back fewer words, and the ones an earlier round handed back keep their decisions only while
    this file still names their sentences."""
    out = {w["wid"]: {**w, "sids": list(w["sids"]), "why": list(w["why"])} for w in old}
    for w in new:
        entry = out.setdefault(w["wid"], {**w, "sids": [], "why": []})
        entry["sids"] = sorted(set(entry["sids"]) | set(w["sids"]))
        entry["why"] = list(dict.fromkeys(entry["why"] + w["why"]))
    return sorted(out.values(), key=lambda w: w["wid"])


def agreement(pairs: list[tuple[str, str, str, str]]) -> tuple[dict, list[str]]:
    """(sid, sentence, a, b) -> exact share, span counts with a as the label, and the
    disagreements listed."""
    counts: Counter[str] = Counter()
    lines = []
    for sid, sentence, a, b in pairs:
        counts["pairs"] += 1
        same = WS_RE.sub("", a) == WS_RE.sub("", b)
        counts["exact"] += same
        comps, unaligned = kanjify_eval.score_row(a, b, sentence)
        counts["unaligned"] += unaligned
        for c in comps:
            counts[c.outcome] += 1
        if not same:
            diffs = [f"{c.outcome}: {c.label or '-'} / {c.out or '-'}  {c.context}"
                     for c in comps if c.outcome != "right"]  # fmt: skip
            lines += [f"- {sid}", f"    a: {a}", f"    b: {b}"] + [f"    {d}" for d in diffs]
    return dict(counts), lines


def figures(c: dict) -> str:
    n = c.get("pairs", 0) or 1
    right = c.get("right", 0)
    p = 100 * right / ((right + c.get("wrong", 0) + c.get("extra", 0)) or 1)
    r = 100 * right / ((right + c.get("wrong", 0) + c.get("missed", 0)) or 1)
    return (
        f"{c.get('pairs', 0)} sentences, exact {100 * c.get('exact', 0) / n:.1f}%; spans right"
        f" {right}, wrong {c.get('wrong', 0)}, missed {c.get('missed', 0)}, extra"
        f" {c.get('extra', 0)} (P {p:.1f}% R {r:.1f}%), unaligned {c.get('unaligned', 0)}"
    )


def cost_lines() -> list[str]:
    lines: list[str] = []
    if not golden.RESULTS.is_dir():
        return lines
    for folder in sorted(p for p in golden.RESULTS.iterdir() if p.is_dir()):
        results = read_results(folder.name)
        errors = len(list(folder.glob("*.error.json")))
        cost = sum(r.get("cost") or 0 for r in results)
        secs = sum(r.get("seconds") or 0 for r in results)
        n = len(results) or 1
        lines.append(
            f"- {folder.name}: {len(results)} done, {errors} failing, ${cost:.2f}"
            f" (${cost / n:.2f} each), {secs / n / 60:.1f} min each"
        )
    return lines


def main() -> int:
    argparse.ArgumentParser().parse_args()
    sentences = {s["sid"]: s for s in golden.read_jsonl(golden.INVENTORY / "sentences.jsonl")}
    words = golden.read_jsonl(golden.INVENTORY / "words.jsonl")
    kana_words = {w["kana"] for w in words} | {w["word"] for w in words}
    golden.COLLATED.mkdir(parents=True, exist_ok=True)

    # step 1
    decided = decisions(read_results("words"))
    golden.write_jsonl(golden.DECISIONS, decided)
    version = golden.decisions_version()

    # step 2
    all_rows: dict[str, list[dict]] = {q: step2_rows(q, sentences) for q in STEP2_QUEUES}
    accepted, pending, rejected, furigana = select(all_rows)
    golden.write_jsonl(golden.COLLATED / "accepted.jsonl",
                       ({k: v for k, v in r.items() if k != "problem"} for r in accepted.values()))  # fmt: skip
    golden.write_jsonl(golden.COLLATED / "pending.jsonl", pending)
    golden.write_jsonl(golden.COLLATED / "rejected.jsonl", rejected)
    golden.write_jsonl(golden.COLLATED / "furigana.jsonl", furigana)
    fixes = fix_list(sentences, furigana)
    golden.write_jsonl(golden.COLLATED / "furigana_fixes.jsonl", fixes)

    # the words handed back
    handed: dict[tuple[str, str], dict] = {}
    for row in pending:
        for p in row["pending"]:
            key = (p.get("word", ""), p.get("reading", ""))
            entry = handed.setdefault(key, {"word": key[0], "kana": key[1], "sids": [], "why": []})
            entry["sids"].append(row["sid"])
            entry["why"].append(p.get("why", ""))
    new_words = []
    for (word, kana), entry in sorted(handed.items(), key=lambda kv: -len(kv[1]["sids"])):
        if re.fullmatch(r"Q\d+.*", " ".join(entry["why"])[:3]) or kana in kana_words:
            continue  # a pending question, or a step 1 word still to be decided
        wid = "q" + golden.sentence_id(f"{word}|{kana}")[1:9]
        new_words.append({"wid": wid, **entry, "sids": sorted(set(entry["sids"]))})
    golden.write_jsonl(golden.COLLATED / "queue.jsonl", new_words)
    golden.write_jsonl(golden.HANDED, merge_handed(golden.read_jsonl(golden.HANDED), new_words))

    # the questions
    q_lines = ["# Policy questions the agents raised", ""]
    for d in decided:
        undecided = [u for u in d["uses"] if u.get("write") == "undecided"]
        if not d["questions"] and not undecided:
            continue
        q_lines.append(f"## {d['kana']} ({d['word']}, {d['pos']}) `{d['wid']}`")
        for q in d["questions"]:
            q_lines.append(f"- **{q['question']}**")
            q_lines += [f"  - option: {o}" for o in q.get("options", [])]
            q_lines += [f"  - example: {e}" for e in q.get("examples", [])]
            q_lines.append(f"  - recommends: {q.get('recommendation', '')}")
            q_lines.append(f"  - blocks: {q.get('blocks', '')}")
        q_lines.append("")
    step2_questions: Counter[str] = Counter()
    for queue in STEP2_QUEUES:
        for r in read_results(queue):
            s = r.get("structured")
            for q in (s or {}).get("questions", []) if isinstance(s, dict) else []:
                step2_questions[q.get("question", "")] += 1
    if step2_questions:
        q_lines += ["## From the sentence labellers", ""]
        q_lines += [f"- ({n}x) {q}" for q, n in step2_questions.most_common()]
    pending_questions = Counter(
        re.match(r"Q\d+", p.get("why", "")).group(0)  # type: ignore[union-attr]
        for row in pending for p in row["pending"] if re.match(r"Q\d+", p.get("why", ""))
    )  # fmt: skip
    if pending_questions:
        q_lines += ["", "## Rows waiting on a pending question", ""]
        q_lines += [f"- {q}: {n} uses" for q, n in sorted(pending_questions.items())]
    (golden.COLLATED / "questions.md").write_text("\n".join(q_lines) + "\n", encoding="utf-8")

    # calibration
    hand = golden.hand_labels(golden.read_sentences())
    report = ["# Kanjify golden set: collated", "",
              f"policy {golden.policy_version()}, decisions {version} ({len(decided)} words)", "",
              "## Results", ""] + cost_lines()  # fmt: skip
    report += [
        "",
        f"step 2 rows: accepted {len(accepted)} of {len(sentences)} sentences, pending"
        f" {len(pending)}, broken furigana {len(furigana)}, rejected {len(rejected)}; words"
        f" handed back {len(new_words)}",
        f"furigana fix list: {len(fixes)} sentences"
        f" ({sum(1 for f in fixes if f['labeller'])} with a labeller's fix)",
        f"rejections: {dict(Counter(r['problem'].split(':')[0][:60] for r in rejected))}",
    ]

    def valid(queue: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for row in all_rows.get(queue, []):
            if not row["problem"]:
                out[row["sid"]].append(row)
        return out

    batch_rows = valid("batches")
    vs_hand = [(sid, sentences[sid]["sentence"], hand[sid]["kanjified"], rows[0]["kanjified"])
               for sid, rows in batch_rows.items() if sid in hand]  # fmt: skip
    c, diff = agreement(vs_hand)
    report += ["", "## Against the hand-fixed labels (a = hand, b = step 2)", "", figures(c)]
    dup_pairs = []
    for sid, rows in batch_rows.items():
        firsts = [r for r in rows if "/b" in r["labeller"]]
        seconds = [r for r in rows if "/d" in r["labeller"]]
        if firsts and seconds:
            dup_pairs.append((sid, sentences[sid]["sentence"], firsts[0]["kanjified"],
                              seconds[0]["kanjified"]))  # fmt: skip
    c2, diff2 = agreement(dup_pairs)
    report += ["", "## Between two labellers (a = batch, b = duplicate batch)", "", figures(c2)]

    pilot: dict[str, dict[str, str]] = defaultdict(dict)
    for queue in ("pilot_single", "pilot_batch"):
        for sid, rows in valid(queue).items():
            pilot[queue][sid] = rows[0]["kanjified"]
    op_valid = {r["sid"]: r["kanjified"] for r in op_rows(sentences)} if read_results("pilot_op") else {}
    pilot["pilot_op"] = op_valid
    pilot_lines: list[str] = []
    if any(pilot.values()):
        report += ["", "## Pilot", ""]
        ways = ["pilot_single", "pilot_batch", "pilot_op"]
        for way in ways:
            pairs = [(sid, sentences[sid]["sentence"], hand[sid]["kanjified"], k)
                     for sid, k in pilot[way].items() if sid in hand]  # fmt: skip
            report.append(f"- {way} vs hand: {figures(agreement(pairs)[0])}")
        for i, a in enumerate(ways):
            for b in ways[i + 1 :]:
                shared = sorted(set(pilot[a]) & set(pilot[b]))
                pairs = [(sid, sentences[sid]["sentence"], pilot[a][sid], pilot[b][sid])
                         for sid in shared]  # fmt: skip
                cc, dd = agreement(pairs)
                report.append(f"- {a} vs {b}: {figures(cc)}")
                pilot_lines += [f"### {a} (a) vs {b} (b)", *dd, ""]
    report += ["", "## Disagreements with the hand-fixed labels", "", *diff]
    report += ["", "## Disagreements between two labellers", "", *diff2]
    if pilot_lines:
        report += ["", "## Pilot disagreements", "", *pilot_lines]
    (golden.COLLATED / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report[: report.index("## Disagreements with the hand-fixed labels")]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
