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
