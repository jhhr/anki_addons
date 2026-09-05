from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any, Optional


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


# Separates the note type from the card type in a fully qualified card type
# name. Same spelling as copy_anywhere's, so a name copied between the two
# addons' configs means the same thing in both.
CARD_TYPE_SEPARATOR = "<::>"


def split_quoted_names(value: str) -> list[str]:
    """Split a `"A", "B"` name list, as note type and card type targets are stored."""
    if not value:
        return []
    return [n for n in value.strip('"').split('", "') if n]


def join_quoted_names(names: list[str]) -> str:
    """Inverse of ``split_quoted_names``; an empty list gives an empty string."""
    kept = [n for n in names if n]
    return '"' + '", "'.join(kept) + '"' if kept else ""


def qualified_card_type_name(note_type_name: str, card_type_name: str) -> str:
    """The name a card type target is stored under: note type plus card type.

    Card type names are only unique within a note type, and a rule can target
    several note types at once, so the note type has to travel with the name.
    """
    return f"{note_type_name}{CARD_TYPE_SEPARATOR}{card_type_name}"


# Interpolation variables describing the card whose review triggered the rule.
# The note-level {{Field}} vocabulary cannot see it -- interpolation resolves
# against a note -- so these are handed in as variables instead.
REVIEWED_CARD_TEMPLATE = "__Reviewed_Card_Template"
REVIEWED_CARD_ORD = "__Reviewed_Card_Ord"


def reviewed_card_variables(card_type_name: str, card_ord: int) -> dict[str, object]:
    """Values for the reviewed-card interpolation variables.

    ``card_ord`` is taken 0-based, as Anki stores it, and exposed 1-based so it
    can be dropped straight into a ``card:N`` search term.
    """
    return {
        REVIEWED_CARD_TEMPLATE: card_type_name,
        REVIEWED_CARD_ORD: card_ord + 1,
    }


def cap_card_ids(
    card_ids: list[int],
    cap: int,
    due_by_id: Optional[dict[int, int]] = None,
) -> tuple[list[int], int]:
    """Trim ``card_ids`` to ``cap`` entries, returning ``(kept, dropped_count)``.

    Without ``due_by_id`` the caller's ordering decides who survives. With it --
    and only once the cap actually bites, which is meant to be rare -- the cards
    due latest are the ones dropped, so a capped run still disperses whatever is
    coming up soonest. Survivors keep the caller's ordering either way.
    """
    if cap <= 0:
        return [], len(card_ids)
    if len(card_ids) <= cap:
        return card_ids, 0
    if due_by_id is None:
        return card_ids[:cap], len(card_ids) - cap
    # An id missing from the map has no known due date; sort it last so a known
    # due date always wins a place over an unknown one.
    unknown_due = max(due_by_id.values(), default=0) + 1
    by_due = sorted(card_ids, key=lambda cid: (due_by_id.get(cid, unknown_due), cid))
    kept = set(by_due[:cap])
    return [cid for cid in card_ids if cid in kept], len(card_ids) - cap


def remaining_note_cards(
    remaining: list[int],
    anchor_card_id: int,
    covered_card_ids: Iterable[int],
) -> list[int]:
    """The note's cards still needing a run of their own after one anchor ran.

    A rule run anchored on one card of a note usually finds the note's other
    cards too, and dispersing those again from their own anchor would redo the
    same group; whichever cards the rule's query returned are therefore done.

    The anchor is dropped whether or not the query returned it. A query that
    deliberately omits the card it was triggered from -- one gated to a card
    type the anchor is not, say -- would otherwise never take itself off the
    queue, and the caller would run it forever.
    """
    covered = set(covered_card_ids)
    covered.add(anchor_card_id)
    return [cid for cid in remaining if cid not in covered]


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
    backlogged: int = 0,
) -> str:
    """One line per rule run. ``backlogged`` is only spelled out when it bit.

    A backlogged card is one the run pinned rather than moved, so a run that
    reports a large candidate count and a small updated count is not losing
    cards silently -- that field says where they went.
    """
    backlog_part = f"backlogged={backlogged}, " if backlogged else ""
    return (
        f"{rule_name}: candidates={candidates}, filtered={filtered_out}, "
        f"capped={capped_out}, {backlog_part}updated={updated}, outcome={outcome}"
    )


# Deck-preset review orders that actually sort by due date, as
# DeckConfig.Config.ReviewCardOrder numbers. Anki hands the deck's review order
# straight to the SQL that gathers due cards, and the daily limit then truncates
# that stream -- so under any *other* order the due date decides only whether a
# card joins today's pool, never where in it the card lands. Moving a due date
# around inside the past is therefore a no-op for every order not listed here.
DUE_ORDERED_REVIEW_ORDERS = frozenset(
    {
        0,  # DAY
        1,  # DAY_THEN_DECK
        2,  # DECK_THEN_DAY
    }
)


def review_order_uses_due(review_order: int) -> bool:
    return review_order in DUE_ORDERED_REVIEW_ORDERS
