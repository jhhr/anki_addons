from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any


def normalize_card_id_result(result: Any) -> list[int]:
    if result is None:
        return []
    if isinstance(result, int):
        return [result]
    if isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            return []
        if stripped.isdigit():
            return [int(stripped)]
        raise ValueError("code result string must be a query or numeric card id")
    if isinstance(result, Iterable):
        out: list[int] = []
        for item in result:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                out.append(item)
                continue
            if isinstance(item, str) and item.strip().isdigit():
                out.append(int(item.strip()))
                continue
            raise ValueError("card-id list must contain only integers")
        return out
    raise ValueError("code result must be query string, int, or iterable of ints")


def dedupe_preserve_order(values: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def split_note_type_names(target_note_types: str) -> list[str]:
    if not target_note_types:
        return []
    return [n for n in target_note_types.strip('"').split('", "') if n]


def cap_card_ids(card_ids: list[int], cap: int) -> tuple[list[int], int]:
    if cap <= 0:
        return [], len(card_ids)
    if len(card_ids) <= cap:
        return card_ids, 0
    return card_ids[:cap], len(card_ids) - cap


def group_overlapping_sets(groups: list[set[int]]) -> list[set[int]]:
    pending = deque(set(g) for g in groups if g)
    merged: list[set[int]] = []
    while pending:
        current = pending.popleft()
        changed = True
        while changed:
            changed = False
            next_pending: list[set[int]] = []
            for maybe in pending:
                if current & maybe:
                    current |= maybe
                    changed = True
                else:
                    next_pending.append(maybe)
            pending = deque(next_pending)
        merged.append(current)
    return merged


def summarize_outcome(
    rule_name: str,
    candidates: int,
    filtered_out: int,
    capped_out: int,
    updated: int,
    outcome: str,
) -> str:
    return (
        f"{rule_name}: candidates={candidates}, filtered={filtered_out}, "
        f"capped={capped_out}, updated={updated}, outcome={outcome}"
    )
