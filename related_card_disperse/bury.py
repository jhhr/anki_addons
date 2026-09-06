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
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from anki.errors import NotFoundError
from anki.utils import ids2str
from aqt import mw

from .configuration import Config
from .core import (
    describe_rule_errors,
    merge_rule_error,
    order_session_blocks,
    rule_display_name,
    select_cards_to_bury,
)
from .logic import (
    ProgressReporter,
    card_type_name_for,
    count_buried_by_deck,
    describe_buried_decks,
    get_applicable_rules,
    resolve_rule_candidates,
)

# Intraday learning, review, and interday learning: everything the deck can put
# in front of you today that is not a new card. New cards have their own gather
# order and their own limit, and a rule that relates them is relating cards with
# no schedule to disperse.
SESSION_QUEUES = (1, 2, 3)
QUEUE_TYPE_REV = 2
QUEUE_TYPE_LRN = 1

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
    # One entry per deck that contributed, as (deck name, ordering, card count),
    # in the order the blocks were concatenated. A single-deck run leaves this
    # empty; the summary line falls back to ``ordering``.
    blocks: list[tuple[str, str, int]] = field(default_factory=list)


@dataclass
class BuryRunResult:
    deck_name: str = ""
    # The block the run anchored on: the filtered deck itself, or the top-level
    # deck the clicked one belongs to. Buries inside this tree are the ones the
    # user expected; everything else is reported as elsewhere.
    block_name: str = ""
    ordering: str = ""
    session_cards: int = 0
    pool_size: int = 0
    limited: bool = False
    buried: int = 0
    rule_runs: int = 0
    cancelled: bool = False
    error: str = ""
    blocks: list[tuple[str, str, int]] = field(default_factory=list)
    # Rule name -> (first error message, how many cards it failed on). A rule
    # whose code raises does so for every card, so the count is what tells a
    # broken rule apart from an unlucky one.
    rule_errors: dict[str, tuple[str, int]] = field(default_factory=dict)
    buried_by_deck: dict[str, int] = field(default_factory=dict)


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


def _is_filtered(deck_id: int) -> bool:
    deck = mw.col.decks.get(deck_id, default=False)
    return bool(deck and deck.get("dyn"))


# Deck id -> (review count, learning count) for today.
DeckLimits = dict[int, tuple[int, int]]


def _node_counts(node: Any) -> tuple[int, int]:
    return int(getattr(node, "review_count", 0)), int(getattr(node, "learn_count", 0))


def deck_limit_map() -> DeckLimits:
    """Today's counts for every deck, from a single tree build.

    ``deck_due_tree(deck_id)`` builds the whole tree and then walks down to the
    one node, so asking it per deck is quadratic in the number of decks. A
    collection-wide session asks about every top-level deck, so it builds once
    and indexes the answer.
    """
    counts: DeckLimits = {}

    def walk(node: Any) -> None:
        counts[int(node.deck_id)] = _node_counts(node)
        for child in node.children:
            walk(child)

    try:
        root = mw.col.sched.deck_due_tree()
    except Exception:
        return counts
    if root is not None:
        walk(root)
    return counts


def _normal_session_order(deck_id: int, limits: Optional[DeckLimits] = None) -> SessionOrder:
    # Filtered children are dropped: a card in one has its queue position in
    # ``due`` and its real due date in ``odue``, so leaving it here would sort a
    # row number against its neighbours' due days. Each filtered deck
    # contributes its own block instead, which is also where the scheduler
    # counts those cards.
    deck_ids = [did for did in mw.col.decks.deck_and_child_ids(deck_id) if not _is_filtered(did)]
    deck_conf = mw.col.decks.config_dict_for_deck_id(deck_id)
    review_order = int(deck_conf.get("reviewOrder", 0))
    fsrs = bool(mw.col.get_config("fsrs", False))

    # Review and interday learning carry a day number in ``due``; intraday
    # learning carries a unix timestamp, which no day comparison can read. An
    # intraday card is mid-step *today* by definition, so it needs no due test
    # -- and it leads the block, because a card you are part-way through is the
    # one you least want burying out from under you.
    due_column = "CASE WHEN odid == 0 THEN due ELSE odue END"
    where = (
        f"did IN {ids2str(deck_ids)}"
        f" AND queue IN {ids2str(SESSION_QUEUES)}"
        f" AND (queue == {QUEUE_TYPE_LRN} OR {due_column} <= ?)"
    )
    learning_first = f"CASE WHEN queue == {QUEUE_TYPE_LRN} THEN 0 ELSE 1 END"

    def query(clause: str) -> list[int]:
        return mw.col.db.list(
            f"SELECT id FROM cards WHERE {where} ORDER BY {learning_first}, {clause}",
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
    if limits is None:
        node = mw.col.sched.deck_due_tree(deck_id)
        counts = None if node is None else _node_counts(node)
    else:
        counts = limits.get(deck_id)
    if counts is None:
        limit = pool_size
    else:
        # Learning cards are not subject to the review limit, and they lead the
        # block, so the cap has to make room for them or it would truncate
        # reviews the deck will really deal.
        limit = counts[0] + counts[1]
    return SessionOrder(
        card_ids=card_ids[:limit],
        ordering=ordering,
        limited=pool_size > limit,
        pool_size=pool_size,
    )


def _block_name_for(deck_id: int) -> str:
    """The block a deck's cards belong to: itself if filtered, else its root.

    A normal subdeck is dealt as part of its top-level tree -- that is the unit
    with a daily limit the user actually meets -- so running the command on
    ``JP vocab::N5`` anchors on ``JP vocab``.
    """
    deck = mw.col.decks.get(deck_id, default=False)
    if deck is None:
        raise ValueError("deck no longer exists")
    name = deck["name"]
    return name if deck.get("dyn") else name.split("::")[0]


def collection_session_order(anchor_deck_id: int) -> SessionOrder:
    """Every deck's session today, concatenated into one order.

    Not "the collection's due cards": each deck still contributes its own pool,
    in its own preset's review order, truncated by its own limit. Only the union
    is new, and it is what lets a rule pointing from one deck tree into another
    find a collision at all -- a session limited to one tree resolves those
    relations and then throws them away, which is why the command could report
    nothing to do on a session full of them.
    """
    anchor_name = _block_name_for(anchor_deck_id)
    limits = deck_limit_map()
    blocks: list[tuple[str, list[int]]] = []
    described: list[tuple[str, str, int]] = []
    pool_size = 0
    limited = False

    for entry in mw.col.decks.all_names_and_ids(skip_empty_default=True, include_filtered=True):
        if _is_filtered(entry.id):
            order = _filtered_session_order(entry.id)
        elif "::" in entry.name:
            # A normal subdeck is already inside its root's block.
            continue
        else:
            order = _normal_session_order(entry.id, limits)
        if not order.card_ids:
            continue
        blocks.append((entry.name, order.card_ids))
        described.append((entry.name, order.ordering, len(order.card_ids)))
        pool_size += order.pool_size
        limited = limited or order.limited

    # Same rule the block concatenation uses, so the summary lists the decks in
    # the order they were actually walked.
    described.sort(key=lambda block: (block[0] != anchor_name, block[0]))
    return SessionOrder(
        card_ids=order_session_blocks(blocks, anchor_name),
        ordering="each deck's own order",
        limited=limited,
        pool_size=pool_size,
        blocks=described,
    )


def session_order_for_deck(deck_id: int, across_decks: bool = True) -> SessionOrder:
    if across_decks:
        return collection_session_order(deck_id)
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
) -> tuple[dict[int, set[int]], int, bool, dict[str, tuple[str, int]]]:
    """Which cards of a session the rules relate to which others.

    Relations are symmetrised: a rule query is written from one card's point of
    view, but two cards landing in one session is not, so a rule that finds B
    from A counts as a collision when B comes first.

    Cards outside the session are dropped rather than followed. The question is
    only what today's session shows together; a related card that is not in it
    cannot collide with anything. Which is why the session has to be the whole
    day across every deck: a session of one deck tree drops every relation that
    points out of it.

    Rule errors come back alongside, one entry per rule rather than per card --
    a rule that raises does so on every card it is asked about.
    """
    session = set(session_ids)
    neighbours: dict[int, set[int]] = {}
    rule_errors: dict[str, tuple[str, int]] = {}
    rule_runs = 0
    total = len(session_ids)
    for index, cid in enumerate(session_ids):
        if report is not None and (index % 5 == 0 or index == total - 1):
            if report(f"Checking card {index + 1}/{total}", index + 1, total):
                return neighbours, rule_runs, True, rule_errors
        try:
            card = mw.col.get_card(cid)
        except NotFoundError:
            continue
        note_type = card.note().note_type()
        if not note_type:
            continue
        rules = get_applicable_rules(
            config.rules_for_model(note_type), note_type["name"], card_type_name_for(card)
        )
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
                # A rule whose code raises is otherwise indistinguishable from
                # one that found nothing, which is how a rule can sit broken for
                # months while the run cheerfully reports no collisions.
                merge_rule_error(rule_errors, rule_display_name(rule), resolution.error)
                continue
            for other in resolution.card_ids:
                if other == cid or other not in session:
                    continue
                neighbours.setdefault(cid, set()).add(other)
                neighbours.setdefault(other, set()).add(cid)
    return neighbours, rule_runs, False, rule_errors


def run_deck_bury_disperse(
    deck_id: int,
    config: Config,
    report: Optional[ProgressReporter] = None,
    across_decks: bool = True,
) -> BuryRunResult:
    result = BuryRunResult()
    try:
        deck = mw.col.decks.get(deck_id, default=False)
        result.deck_name = deck["name"] if deck else str(deck_id)
        result.block_name = _block_name_for(deck_id) if deck else result.deck_name
        session = session_order_for_deck(deck_id, across_decks)
    except Exception as exc:
        result.error = str(exc)
        return result

    result.ordering = session.ordering
    result.session_cards = len(session.card_ids)
    result.pool_size = session.pool_size
    result.limited = session.limited
    result.blocks = session.blocks
    if len(session.card_ids) < 2:
        return result

    neighbours, rule_runs, cancelled, rule_errors = session_relations(
        session.card_ids, config, report
    )
    result.rule_runs = rule_runs
    result.cancelled = cancelled
    result.rule_errors = rule_errors
    if cancelled:
        return result

    _, to_bury = select_cards_to_bury(session.card_ids, neighbours, config.bury_min_gap)
    if not to_bury:
        return result

    result.buried_by_deck = count_buried_by_deck(to_bury)

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
    across_decks: bool = True,
) -> None:
    """Run a deck's session dispersal off the UI thread.

    A session is a query per card per rule, which is the same cost that made the
    browser and sync runs need a progress dialog.
    """

    def report(label: str, value: int, maximum: int) -> bool:
        mw.taskman.run_on_main(lambda: mw.progress.update(label=label, value=value, max=maximum))
        return mw.progress.want_cancel()

    def task() -> BuryRunResult:
        return run_deck_bury_disperse(deck_id, config, report, across_decks)

    def done(future) -> None:
        mw.progress.finish()
        result = future.result()
        mw.reset()
        on_done(result)

    mw.progress.start(label="Dispersing due cards", parent=parent, immediate=False)
    mw.taskman.run_in_background(task, done)


MAX_BLOCKS_SHOWN = 4


def _describe_blocks(result: BuryRunResult) -> str:
    """The decks that contributed, or the single deck's ordering."""
    if len(result.blocks) <= 1:
        return f"Ordered by {result.ordering} ({result.rule_runs} rule runs)"
    shown = result.blocks[:MAX_BLOCKS_SHOWN]
    parts = "; ".join(f"{name}: {count} by {ordering}" for name, ordering, count in shown)
    hidden = len(result.blocks) - len(shown)
    if hidden:
        parts += f"; and {hidden} more deck{'' if hidden == 1 else 's'}"
    return f"{parts} ({result.rule_runs} rule runs)"


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
    lines.append(_describe_blocks(result))
    if result.limited:
        lines.append(
            f"{result.pool_size} cards are due; the daily limit stops at {result.session_cards},"
            " so only those were considered. Limits are counted per top-level deck, so a deck"
            " that sets its own per-subdeck limits may deal a slightly different session"
        )
    if not result.buried:
        lines.append("No two related cards were going to come up together")

    # Buries inside the tree the run was started on are what the user asked for;
    # the rest are decks they were not looking at and would otherwise find short.
    prefix = f"{result.block_name}::"
    outside = {
        name: count
        for name, count in result.buried_by_deck.items()
        if name != result.block_name and not name.startswith(prefix)
    }
    elsewhere = describe_buried_decks(outside, current_deck=result.block_name)
    if elsewhere:
        lines.append(elsewhere)

    if result.rule_errors:
        lines.append(describe_rule_errors(result.rule_errors))
    return "<br>".join(lines)
