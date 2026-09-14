"""Words of the judge's eval judged by hand, in a file of their own.

`hand_judge.py` writes them and `judge_eval.py build` lays them over the checked export's labels.
A label names its word by the sentence (context stripped, as the eval set has it), the dictionary
forms from the outermost word down to it (`path`), which occurrence of that path it is, and the
reading, rather than by position: the labels are to outlive changes to the generator, and a word
the generator no longer makes is reported as stale instead of landing on some other word.

    {"sentence", "path": [..., dict_form], "occurrence", "reading", "pos", "group", "label"}
"""

import json
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from _bootstrap import ADDON_ROOT

HAND_LABELS = ADDON_ROOT / "output" / "word_matching_judge_hand_labels.jsonl"


class Placed(NamedTuple):
    index: int  # in iter_words order
    parents: list[list]  # outermost first
    elem: list
    path: tuple[str, ...]
    occurrence: int


def placed_words(arr: list) -> list[Placed]:
    """Every word of `arr` in `iter_words` order, with where it sits."""
    out: list[Placed] = []
    seen: Counter[tuple[str, ...]] = Counter()

    def walk(items: list, parents: list[list]) -> None:
        for elem in items:
            if len(elem) > 1:
                path = tuple(p[2] for p in parents) + (elem[2],)
                out.append(Placed(len(out), parents, elem, path, seen[path]))
                seen[path] += 1
                walk(elem[5], parents + [elem])

    walk(arr, [])
    return out


def label_key(row: dict) -> tuple:
    return (row["sentence"], tuple(row["path"]), row["occurrence"], row["reading"])


def placed_key(sentence: str, placed: Placed) -> tuple:
    return (sentence, placed.path, placed.occurrence, placed.elem[3])


def make_label(sentence: str, placed: Placed, group: str, label: str) -> dict:
    return {
        "sentence": sentence,
        "path": list(placed.path),
        "occurrence": placed.occurrence,
        "reading": placed.elem[3],
        "pos": placed.elem[1],
        "group": group,
        "label": label,
    }


def read_labels(path: Path = HAND_LABELS) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def write_labels(rows: list[dict], path: Path = HAND_LABELS) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_export_nids(path: Path) -> dict[str, list[int]]:
    """The note ids of every raw sentence of a migration export, context kept as the export has
    it. A row written before the export carried `nids` gives none."""
    out: dict[str, list[int]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        nids = out.setdefault(row["sentence"], [])
        nids.extend(nid for nid in row.get("nids", []) if nid not in nids)
    return {sentence: nids for sentence, nids in out.items() if nids}


def move_labels(labels: list[dict], old: str, new: str, arr: list) -> tuple[list[dict], int, int]:
    """The labels with those of sentence `old` put on `new`, whose array is `arr`: a label whose
    word `arr` no longer has is dropped, and a moved label replaces one `new` already had.
    Returns the labels and how many moved and were dropped."""
    present = {(p.path, p.occurrence, p.elem[3]) for p in placed_words(arr)}
    leaving = [row for row in labels if row["sentence"] == old]
    moved = [
        {**row, "sentence": new}
        for row in leaving
        if (tuple(row["path"]), row["occurrence"], row["reading"]) in present
    ]
    keys = {label_key(row) for row in moved}
    kept = [row for row in labels if row["sentence"] != old and label_key(row) not in keys]
    return kept + moved, len(moved), len(leaving) - len(moved)


def rewrite_rows(path: Path, new_raw: dict[int, str], renamed: dict[str, str]) -> int:
    """Rewrite a migration export (or the checked subset) after notes' sentence fields changed
    in Anki. `new_raw` is the edited notes' new raw field by note id, `renamed` the old raw
    sentence → the new one, for rows without `nids`. A row whose notes now differ splits into a
    row per text, each keeping the word list; rows left with one sentence merge as the export
    makes them, the first word list winning. Written through a temp file. Returns the rows
    changed."""
    if not path.exists():
        return 0
    rows, changed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        nids = row.get("nids") or []
        if any(nid in new_raw for nid in nids):
            by_text: dict[str, list[int]] = {}
            for nid in nids:
                by_text.setdefault(new_raw.get(nid, row["sentence"]), []).append(nid)
            rows.extend({**row, "sentence": text, "nids": ids} for text, ids in by_text.items())
            changed += 1
        elif not nids and row["sentence"] in renamed:
            rows.append({**row, "sentence": renamed[row["sentence"]]})
            changed += 1
        else:
            rows.append(row)
    if not changed:
        return 0
    merged: dict[str, dict] = {}
    for row in rows:
        first = merged.setdefault(row["sentence"], row)
        if first is not row and "nids" in row:
            ids = first.setdefault("nids", [])
            ids.extend(nid for nid in row["nids"] if nid not in ids)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in merged.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return changed


def apply_labels(arr: list, expected: list, labels: list[dict]) -> tuple[list[int], int, int]:
    """Write `labels` (all of one sentence) into `expected`, which is in `iter_words` order.
    Returns the indices labelled by hand, how many of them changed a label that was there,
    and how many labels found no word."""
    by_key = {(p.path, p.occurrence, p.elem[3]): p.index for p in placed_words(arr)}
    hand, changed, stale = [], 0, 0
    for row in labels:
        index = by_key.get((tuple(row["path"]), row["occurrence"], row["reading"]))
        if index is None:
            stale += 1
            continue
        if expected[index] is not None and expected[index] != row["label"]:
            changed += 1
        expected[index] = row["label"]
        hand.append(index)
    return sorted(set(hand)), changed, stale
