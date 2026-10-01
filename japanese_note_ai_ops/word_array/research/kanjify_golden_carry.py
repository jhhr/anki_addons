"""Carries the kanjify golden set's labels over to sentences that changed in the notes by a
furigana repair alone, and lists the other changed sentences for a relabel round.

A sentence is named by a hash of its text (`kanjify_golden.sentence_id`), so a note whose
furigana was fixed (`furigana_fix.py`) holds a sentence with a new id and no label. Run after
`kanjify_golden_dump.py` wrote the new dump, keeping the old one as OLD_DUMP, and before the
collate step, whose `accepted.jsonl` still labels the old sentences:

    py -3.10 word_array/research/kanjify_golden_carry.py OLD_DUMP [--relabel SIDS_FILE]

For each sentence of the new dump the old one lacks:
- when every one of its notes held one old sentence and the new one is that sentence with only
  the program's repairs (`kanjify_golden.fix_groups`: a space before a group, katakana split out
  of a group), and the old sentence has an accepted label, the label gets the same repairs and,
  if it passes `kanjify_golden.row_problem` against the new sentence, is saved as a relabel
  result (`results/relabel/r<time>-carried-<n>.json`, one file per policy version the labels were
  made on, so each row keeps its own). The repairs change no reading, and the kana they free
  from a group were nearly all particles and okurigana the label kept in kana anyway;
- else it goes to the relabel list (SIDS_FILE), for `kanjify_golden_render.py relabel`: a
  reading fix, a note changed by hand, a new note.
Handed-back words (`handed_back.jsonl`) are pointed at their old sentences' new ids, so the
relabel prompts still carry their decisions.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import kanjify_golden as golden


def old_sentence_of(new_nids: list[int], by_nid: dict[int, str]) -> str | None:
    """The one old sentence the notes held, or None when they held none or several."""
    old = {by_nid.get(nid) for nid in new_nids}
    return old.pop() if len(old) == 1 and None not in old else None


def carry(label: str, new: str) -> str | None:
    """The old label with the repairs the sentence got, if it labels the new sentence."""
    carried = golden.fix_groups(label)
    return carried if golden.row_problem(new, carried) is None else None


def plan(
    old_dump: list[dict], new_sentences: list[golden.Sentence], accepted: dict[str, dict]
) -> tuple[dict[str, list[dict]], list[str], dict[str, set]]:
    """(carried rows by the policy version they were made on, the sids to relabel, the new sids
    of each old one)."""
    by_nid = {row["nid"]: row["sentence"] for row in old_dump if row["sentence"]}
    old_sids = {golden.sentence_id(s) for s in by_nid.values()}
    carried: dict[str, list[dict]] = defaultdict(list)
    relabel, moved = [], defaultdict(set)
    for s in new_sentences:
        if s.sid in old_sids:
            continue
        old = old_sentence_of(s.nids, by_nid)
        if old is not None:
            moved[golden.sentence_id(old)].add(s.sid)
        row = accepted.get(golden.sentence_id(old)) if old is not None else None
        if old is not None and row is not None and golden.fix_groups(old) == s.sentence:
            label = carry(row["kanjified"], s.sentence)
            if label is not None:
                carried[row.get("policy_version") or "?"].append(
                    {"sid": s.sid, "kanjified": label, "spans": row.get("spans", []),
                     "pending": [], "furigana": [], "carried_from": row["sid"]}  # fmt: skip
                )
                continue
        relabel.append(s.sid)
    return carried, relabel, moved


def result(id_: str, rows: list[dict], policy_version: str) -> dict:
    """A relabel result in agent_queue's form, holding labels carried over, not an agent's."""
    return {"id": id_, "structured": {"rows": rows}, "cost": 0, "seconds": 0,
            "meta": {"kind": "carried", "policy_version": policy_version,
                     "decisions_version": golden.decisions_version(),
                     "sids": [r["sid"] for r in rows]},
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z")}  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("old_dump", type=Path)
    parser.add_argument("--relabel", type=Path, default=golden.COLLATED / "relabel_sids.txt")
    args = parser.parse_args()

    accepted = {r["sid"]: r for r in golden.read_jsonl(golden.COLLATED / "accepted.jsonl")}
    carried, relabel, moved = plan(golden.read_jsonl(args.old_dump), golden.read_sentences(),
                                   accepted)  # fmt: skip
    folder = golden.RESULTS / "relabel"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%y%m%d%H%M")
    for n, (version, rows) in enumerate(sorted(carried.items()), 1):
        path = folder / f"r{stamp}-carried-{n}.json"
        path.write_text(json.dumps(result(path.stem, rows, version), ensure_ascii=False, indent=1),
                        encoding="utf-8")  # fmt: skip
    args.relabel.write_text("".join(sid + "\n" for sid in relabel), encoding="utf-8")
    handed = golden.read_jsonl(golden.HANDED)
    for h in handed:
        h["sids"] = sorted({new for sid in h["sids"] for new in moved.get(sid, {sid})})
    golden.write_jsonl(golden.HANDED, handed)
    print(f"carried {sum(len(r) for r in carried.values())} labels, {len(relabel)} sentences to"
          f" relabel -> {args.relabel}")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
