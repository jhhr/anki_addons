from __future__ import annotations

from anki.cards import Card, CardId
from aqt import mw
from aqt.gui_hooks import reviewer_did_answer_card

from .configuration import Config
from .logic import (
    NOTIFY_SOURCE,
    RuleOutcome,
    StatsCache,
    card_type_name_for,
    describe_buried_decks,
    get_applicable_rules,
    merge_buried_by_deck,
    run_rule_for_reviewed_card,
)
from .shared.notify import post


def _home_deck_name(card: Card) -> str:
    """The deck the card belongs to, filtered decks resolved to the real one."""
    return mw.col.decks.name(card.odid if card.odid else card.did)


def run_related_disperse_on_review(_reviewer, card: Card, _ease) -> None:
    config = Config()
    config.load()

    note = card.note()
    note_type = note.note_type()
    if not note_type:
        return

    rules = get_applicable_rules(
        config.rules_for_model(note_type),
        note_type["name"],
        card_type_name_for(card),
        on_review=True,
    )
    if not rules:
        return

    answer_undo_entry = mw.col.undo_status().last_step
    stats_cache: StatsCache = {}
    processed_rule_card_pairs: set[tuple[str, CardId]] = set()

    outcomes = []
    for rule in rules:
        outcome = run_rule_for_reviewed_card(
            rule,
            card,
            config,
            stats_cache,
            answer_undo_entry,
            processed_rule_card_pairs,
        )
        outcomes.append(outcome)

    if config.hide_review_report:
        return

    reportable = [o for o in outcomes if o.updated > 0 or not config.hide_review_unchanged]
    if not reportable:
        return

    # A rule reaches wherever its query points, so answering one card can bury
    # related cards sitting in decks the user is not studying. That deck is just
    # quietly shorter tomorrow unless the report names it, so it gets its own
    # line -- under both report styles, since it is news either way.
    buried_by_deck: dict[str, int] = {}
    for outcome in reportable:
        merge_buried_by_deck(buried_by_deck, outcome.buried_by_deck)
    elsewhere = describe_buried_decks(buried_by_deck, current_deck=_home_deck_name(card))
    post_review_report(outcomes, reportable, elsewhere, not config.hide_review_details)


def post_review_report(
    outcomes: list[RuleOutcome], reportable: list[RuleOutcome], elsewhere: str, details: bool
) -> None:
    """Report an answer's dispersal: how many cards in the title, the rest behind it.

    The title is the line the compact style always showed, so both styles say at a
    glance what the answer did; the detailed one adds each rule's line below it.
    """
    total = sum(o.updated for o in reportable)
    runs = len(outcomes)
    lines = [o.message for o in reportable] if details else []
    if elsewhere:
        lines.append(elsewhere)
    post(
        source=NOTIFY_SOURCE,
        title=(
            f"Dispersed {total} card{'' if total == 1 else 's'}"
            f" ({runs} rule run{'' if runs == 1 else 's'})"
        ),
        body="<br><br>".join(lines),
        level="success" if total else "info",
        timeout_ms=7000,
        # One entry, each answer's report replacing the last, as the tooltip was. Kept
        # apart, a session's worth would queue behind the cards on show and go on
        # coming up long after the session ended.
        key="review",
    )


def init_review_hook() -> None:
    reviewer_did_answer_card.append(run_related_disperse_on_review)
