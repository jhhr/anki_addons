from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
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
    buried: int = 0,
) -> str:
    """One line per rule run. ``backlogged`` and ``buried`` only when they bit.

    A backlogged card is one the run pinned rather than moved, so a run that
    reports a large candidate count and a small updated count is not losing
    cards silently -- those two fields say where they went. ``buried`` is the
    subset of the backlog the run took out of today's session, which is the
    only thing that disperses a card whose due date is already in the past.
    """
    backlog_part = f"backlogged={backlogged}, " if backlogged else ""
    buried_part = f"buried={buried}, " if buried else ""
    return (
        f"{rule_name}: candidates={candidates}, filtered={filtered_out}, "
        f"capped={capped_out}, {backlog_part}{buried_part}updated={updated}, outcome={outcome}"
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


def select_cards_to_bury(
    order: list[int],
    neighbours: dict[int, set[int]],
    min_gap: int = 0,
) -> tuple[list[int], list[int]]:
    """Split a session into the cards to keep and the cards to bury.

    ``order`` is the session in the order the deck will show it, and
    ``neighbours`` maps a card to the related cards that are also in the
    session. Walking in that order and burying any card that already has a
    related card kept ahead of it leaves a maximal run with no two related
    cards in it, and keeps whichever member of each collision the deck would
    have shown first.

    ``min_gap`` counts *kept* cards, because a buried card is not shown and so
    does not push the ones behind it any further apart: with a gap of 10 a card
    is buried only when a related card sits within the last ten cards that
    survived. A gap of 0 means the whole session, i.e. one card per group.
    """
    kept: list[int] = []
    kept_index: dict[int, int] = {}
    buried: list[int] = []
    for cid in order:
        collides = False
        for other in neighbours.get(cid, ()):
            index = kept_index.get(other)
            if index is None:
                continue
            if min_gap <= 0 or (len(kept) - index) < min_gap:
                collides = True
                break
        if collides:
            buried.append(cid)
        else:
            kept_index[cid] = len(kept)
            kept.append(cid)
    return kept, buried


def rule_display_name(rule: Mapping[str, Any]) -> str:
    """The name to show for a rule, falling back to a short slice of its guid.

    In core rather than logic because every layer that reports on a rule needs
    it, and a rule error from a deck run should read the same as one from a
    review.
    """
    return rule.get("name") or f"Rule {rule.get('guid', '')[:8]}"


def describe_rule_errors(rule_errors: dict[str, tuple[str, int]]) -> str:
    """One line naming every rule that failed, with its first message.

    A rule that raises fails identically on every card, so what the user needs
    is the rule's name, one copy of the message, and the scale -- not one line
    per card.
    """
    parts = [
        f"{name} ({message}) on {count} card{'' if count == 1 else 's'}"
        for name, (message, count) in sorted(rule_errors.items())
    ]
    return "Rule errors: " + "; ".join(parts)


def order_session_blocks(
    blocks: list[tuple[str, list[int]]],
    anchor_deck: str,
) -> list[int]:
    """One session order out of several decks' sessions.

    There is no true global order across decks -- the user decides which deck to
    open, and each deck deals its own pool in its own preset's order -- so the
    union is a concatenation of per-deck blocks rather than one re-sort. The
    block order carries the only preference there is: the deck the run was
    started from leads, so a collision between it and another deck buries the
    *other* deck's card. Running the command on a vocab deck should thin the
    kanji deck, not the vocab deck the user is about to sit down with.

    The rest follow in deck-name order, which is arbitrary but stable, so two
    runs over an unchanged collection bury the same cards.

    A card is kept once, in the first block that claims it. An ``anchor_deck``
    that names no block is not an error: the deck the run started from may have
    nothing due today, and the remaining blocks still order deterministically.
    """
    ordered = sorted((name, ids) for name, ids in blocks if ids)
    lead = [(name, ids) for name, ids in ordered if name == anchor_deck]
    rest = [(name, ids) for name, ids in ordered if name != anchor_deck]

    session: list[int] = []
    seen: set[int] = set()
    for _, ids in lead + rest:
        for cid in ids:
            if cid in seen:
                continue
            seen.add(cid)
            session.append(cid)
    return session


def merge_rule_error(
    errors: dict[str, tuple[str, int]],
    rule_name: str,
    message: str,
) -> None:
    """Record one rule's failure, keeping the first message and a count.

    A rule whose code raises does so for every card it is asked about, so a
    broken rule over a 300-card session would otherwise report 300 identical
    lines. The first message is the informative one; the count is what says the
    rule is broken rather than unlucky.
    """
    previous = errors.get(rule_name)
    if previous is None:
        errors[rule_name] = (message, 1)
    else:
        errors[rule_name] = (previous[0], previous[1] + 1)


def select_backlog_cards_to_bury(
    past_due: list[int],
    anchor_id: Optional[int] = None,
    slot_taken: bool = False,
) -> list[int]:
    """Which of one group's past-due cards to bury, keeping at most one.

    ``past_due`` holds the group's cards that are already in today's pool --
    due today or earlier -- and still live, most overdue first. A due date
    cannot disperse these. Anki pools everything with ``due <= today`` and
    orders that pool by the deck's review order, so moving a date around inside
    the pool decides nothing, and moving it out of the pool postpones a card
    that is due now. Burying is the only lever left, it leaves the schedule
    untouched, and it is the one Anki itself pulls on the siblings of the card
    you just answered.

    A group gets one card a day. The anchor claims that slot when it is itself
    past due, being the card the run was started from; otherwise the card that
    has waited longest claims it. ``slot_taken`` says the group has already had
    its card today -- something in it was answered, here or on another device
    -- and then the whole live backlog goes.

    Unlike a deck run, this has no session order to count a gap against, so it
    always applies the one-a-day rule rather than ``bury_min_gap``: the ordering
    that setting measures against only exists once a deck builds its queue.
    """
    if not past_due:
        return []
    if slot_taken:
        return list(past_due)
    keeper = anchor_id if anchor_id in past_due else past_due[0]
    return [cid for cid in past_due if cid != keeper]
