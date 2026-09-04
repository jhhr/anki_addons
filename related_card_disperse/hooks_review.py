from __future__ import annotations

from anki.cards import Card
from aqt import mw
from aqt.gui_hooks import reviewer_did_answer_card
from aqt.utils import tooltip

from .configuration import Config
from .logic import get_applicable_rules, run_rule_for_reviewed_card


def run_related_disperse_on_review(_reviewer, card: Card, _ease) -> None:
    config = Config()
    config.load()

    note = card.note()
    note_type = note.note_type()
    if not note_type:
        return

    rules = get_applicable_rules(config.rules, note_type["name"], on_review=True)
    if not rules:
        return

    answer_undo_entry = mw.col.undo_status().last_step
    stats_cache = {}
    processed_rule_card_pairs: set[tuple[str, int]] = set()

    messages: list[str] = []
    for rule in rules:
        outcome = run_rule_for_reviewed_card(
            rule,
            card,
            config,
            stats_cache,
            answer_undo_entry,
            processed_rule_card_pairs,
        )
        if outcome.updated > 0 or config.show_unchanged_outcome:
            messages.append(outcome.message)

    if messages:
        tooltip("<br><br>".join(messages), period=7000)


def init_review_hook() -> None:
    reviewer_did_answer_card.append(run_related_disperse_on_review)
