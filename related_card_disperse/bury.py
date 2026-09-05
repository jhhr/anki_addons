"""Disperse a day's study session by burying colliding cards.

Rescheduling disperses the future. It cannot disperse a backlog: Anki gathers
every card with ``due <= today`` into one pool, orders that pool by the deck's
review order, and lets the daily limit truncate the result -- so unless that
order is a due-date one, a backlogged card's due date decides only whether it
joins the pool, never where in it the card lands. Burying works on the pool
itself, is unaffected by the review order, survives moving cards between decks
and rebuilding filtered decks, changes no scheduling data, and Anki lifts it by
itself at the day rollover. For a session, it is the only honest lever.

The run is idempotent: a buried card leaves the session, and what is left has no
two related cards in it, so running it again finds nothing to do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from anki.errors import NotFoundError
from anki.utils import ids2str
from aqt import mw

from .configuration import Config
from .core import select_cards_to_bury
from .logic import (
    ProgressReporter,
    card_type_name_for,
    get_applicable_rules,
    resolve_rule_candidates,
)

# Intraday learning, review, and interday learning: everything the deck can put
# in front of you today that is not a new card. New cards have their own gather
# order and their own limit, and a rule that relates them is relating cards with
# no schedule to disperse.
SESSION_QUEUES = (1, 2, 3)
QUEUE_TYPE_REV = 2

REVIEW_ORDER_NAMES = {
    0: "due date",
    1: "due date, then deck",
    2: "deck, then due date",
    3: "ascending intervals",
    4: "descending intervals",
    5: "ascending ease",
    6: "descending ease",
    7: "ascending retrievability",
    8: "random",
    9: "order added",
    10: "reverse order added",
    11: "descending retrievability",
}


@dataclass
class SessionOrder:
    """The cards a deck will show today, in the order it will show them."""

    card_ids: list[int]
    ordering: str
    # True when the daily limit cut the pool down, i.e. the deck holds more due
    # cards than this session will reach.
    limited: bool = False
    pool_size: int = 0


@dataclass
class BuryRunResult:
    deck_name: str = ""
    ordering: str = ""
    session_cards: int = 0
    pool_size: int = 0
    limited: bool = False
    buried: int = 0
    rule_runs: int = 0
    cancelled: bool = False
    error: str = ""


def _review_order_clause(review_order: int, fsrs: bool) -> str:
    """Anki's own ORDER BY for a deck preset's review order.

    Copied clause for clause from ``review_order_sql`` in rslib, down to the
    ``fnvhash`` tiebreak, so the session this predicts is the session the deck
    will actually deal out. The custom SQL functions it leans on are registered
    on the collection's connection, so they are usable from here.

    The two deck-position orders are the exception: Anki sorts those by the
    row number of the deck in the queue builder's ``active_decks`` temp table,
    which does not exist outside a queue build. Deck id stands in for it, which
    keeps cards of a deck together but can order the decks differently.
    """
    today = mw.col.sched.today
    next_day_at = mw.col.sched.day_cutoff
    now = int(time.time())
    retrievability = (
        "extract_fsrs_retrievability(data,"
        " CASE WHEN odue != 0 THEN odue ELSE due END,"
        f" ivl, {today}, {next_day_at}, {now})"
    )
    clauses = {
        0: "due",
        1: "due, did",
        2: "did, due",
        3: "ivl ASC",
        4: "ivl DESC",
        5: "extract_fsrs_variable(data, 'd') DESC" if fsrs else "factor ASC",
        6: "extract_fsrs_variable(data, 'd') ASC" if fsrs else "factor DESC",
        7: f"{retrievability} ASC",
        8: "",
        9: "nid ASC, ord ASC",
        10: "nid DESC, ord ASC",
        11: f"{retrievability} DESC",
    }
    head = clauses.get(review_order, "due")
    return f"{head}, fnvhash(id, mod)" if head else "fnvhash(id, mod)"


def _filtered_session_order(deck_id: int) -> SessionOrder:
    """A filtered deck's session, which it already wrote down for us.

    Filling a filtered deck rewrites each card's ``due`` with its row number in
    the deck's own search order and parks the real due date in ``odue``
    (``move_into_filtered_deck``). A filtered deck has no preset, so the queue
    builder's sort options fall back to ordering reviews by ``due`` -- which is
    now that row number. So the position column is the session order, whichever
    "cards selected by" setting built the deck, and there is nothing to model.
    """
    card_ids = mw.col.db.list(
        f"SELECT id FROM cards WHERE did = ? AND queue IN {ids2str(SESSION_QUEUES)}"
        " ORDER BY due, id",
        deck_id,
    )
    return SessionOrder(
        card_ids=card_ids,
        ordering="the filtered deck's build order",
        pool_size=len(card_ids),
    )


def _normal_session_order(deck_id: int) -> SessionOrder:
    deck_ids = list(mw.col.decks.deck_and_child_ids(deck_id))
    deck_conf = mw.col.decks.config_dict_for_deck_id(deck_id)
    review_order = int(deck_conf.get("reviewOrder", 0))
    fsrs = bool(mw.col.get_config("fsrs", False))

    def query(clause: str) -> list[int]:
        return mw.col.db.list(
            f"SELECT id FROM cards WHERE did IN {ids2str(deck_ids)}"
            f" AND queue = {QUEUE_TYPE_REV} AND due <= ? ORDER BY {clause}",
            mw.col.sched.today,
        )

    ordering = REVIEW_ORDER_NAMES.get(review_order, "due date")
    try:
        card_ids = query(_review_order_clause(review_order, fsrs))
    except Exception:
        # An Anki old enough to be missing one of the FSRS SQL helpers. Due
        # order is wrong for this deck, but it is the one ordering that always
        # parses, and a session in the wrong order still finds the same
        # collisions -- it just may keep the other member of a pair.
        card_ids = query("due, fnvhash(id, mod)")
        ordering = f"{ordering} (approximated by due date)"

    pool_size = len(card_ids)
    node = mw.col.sched.deck_due_tree(deck_id)
    limit = node.review_count if node is not None else pool_size
    return SessionOrder(
        card_ids=card_ids[:limit],
        ordering=ordering,
        limited=pool_size > limit,
        pool_size=pool_size,
    )


def session_order_for_deck(deck_id: int) -> SessionOrder:
    deck = mw.col.decks.get(deck_id, default=False)
    if deck is None:
        raise ValueError("deck no longer exists")
    if deck.get("dyn"):
        return _filtered_session_order(deck_id)
    return _normal_session_order(deck_id)


def session_relations(
    session_ids: list[int],
    config: Config,
    report: Optional[ProgressReporter] = None,
) -> tuple[dict[int, set[int]], int, bool]:
    """Which cards of a session the rules relate to which others.

    Relations are symmetrised: a rule query is written from one card's point of
    view, but two cards landing in one session is not, so a rule that finds B
    from A counts as a collision when B comes first.

    Cards outside the session are dropped rather than followed. The question is
    only what today's session shows together; a related card that is not in it
    cannot collide with anything.
    """
    session = set(session_ids)
    neighbours: dict[int, set[int]] = {}
    rule_runs = 0
    total = len(session_ids)
    for index, cid in enumerate(session_ids):
        if report is not None and (index % 5 == 0 or index == total - 1):
            if report(f"Checking card {index + 1}/{total}", index + 1, total):
                return neighbours, rule_runs, True
        try:
            card = mw.col.get_card(cid)
        except NotFoundError:
            continue
        note_type = card.note().note_type()
        if not note_type:
            continue
        rules = get_applicable_rules(config.rules, note_type["name"], card_type_name_for(card))
        for rule in rules:
            rule_runs += 1
            # No cap: the cap bounds how many cards one run will reschedule,
            # and nothing is being rescheduled. Intersecting with the session
            # bounds the result far more tightly than the cap would.
            resolution = resolve_rule_candidates(
                rule,
                card,
                config,
                apply_cap=False,
                require_review_state=False,
            )
            if resolution.error:
                continue
            for other in resolution.card_ids:
                if other == cid or other not in session:
                    continue
                neighbours.setdefault(cid, set()).add(other)
                neighbours.setdefault(other, set()).add(cid)
    return neighbours, rule_runs, False


def run_deck_bury_disperse(
    deck_id: int,
    config: Config,
    report: Optional[ProgressReporter] = None,
) -> BuryRunResult:
    result = BuryRunResult()
    try:
        deck = mw.col.decks.get(deck_id, default=False)
        result.deck_name = deck["name"] if deck else str(deck_id)
        session = session_order_for_deck(deck_id)
    except Exception as exc:
        result.error = str(exc)
        return result

    result.ordering = session.ordering
    result.session_cards = len(session.card_ids)
    result.pool_size = session.pool_size
    result.limited = session.limited
    if len(session.card_ids) < 2:
        return result

    neighbours, rule_runs, cancelled = session_relations(session.card_ids, config, report)
    result.rule_runs = rule_runs
    result.cancelled = cancelled
    if cancelled:
        return result

    _, to_bury = select_cards_to_bury(session.card_ids, neighbours, config.bury_min_gap)
    if not to_bury:
        return result

    # One backend call, so the whole dispersal is one undo step, and buried as
    # the scheduler rather than as the user: this is the same job Anki's own
    # sibling burying does, and it keeps "Unbury > Manually buried" meaning what
    # the user did by hand.
    mw.col.sched.bury_cards(to_bury, manual=False)
    result.buried = len(to_bury)
    return result


def run_deck_bury_disperse_in_background(
    deck_id: int,
    config: Config,
    on_done: Callable[[BuryRunResult], None],
    parent: Optional[Any] = None,
) -> None:
    """Run a deck's session dispersal off the UI thread.

    A session is a query per card per rule, which is the same cost that made the
    browser and sync runs need a progress dialog.
    """

    def report(label: str, value: int, maximum: int) -> bool:
        mw.taskman.run_on_main(lambda: mw.progress.update(label=label, value=value, max=maximum))
        return mw.progress.want_cancel()

    def task() -> BuryRunResult:
        return run_deck_bury_disperse(deck_id, config, report)

    def done(future) -> None:
        mw.progress.finish()
        result = future.result()
        mw.reset()
        on_done(result)

    mw.progress.start(label="Dispersing due cards", parent=parent, immediate=False)
    mw.taskman.run_in_background(task, done)


def describe_result(result: BuryRunResult) -> str:
    if result.error:
        return f"Could not disperse: {result.error}"
    if result.cancelled:
        return "Cancelled; nothing was buried"
    if result.session_cards < 2:
        return f"{result.deck_name}: nothing due to disperse"

    lines = [
        f"{result.deck_name}: buried {result.buried} of {result.session_cards}"
        f" card{'' if result.session_cards == 1 else 's'} in today's session"
    ]
    lines.append(f"Ordered by {result.ordering} ({result.rule_runs} rule runs)")
    if result.limited:
        lines.append(
            f"{result.pool_size} cards are due; the daily limit stops at {result.session_cards},"
            " so only those were considered"
        )
    if not result.buried:
        lines.append("No two related cards were going to come up together")
    return "<br>".join(lines)
