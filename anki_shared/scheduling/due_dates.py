"""Revlog and due-date helpers shared by the scheduling addons.

Extracted from custom_schedule_helper's utils.py so that any addon rescheduling
a card can reuse them.
"""

import math
from datetime import datetime, timedelta
from typing import List

from anki.cards import Card
from anki.stats import REVLOG_CRAM
from anki.stats_pb2 import CardStatsResponse
from aqt import mw


def filter_revlogs(
    revlogs: List[CardStatsResponse.StatsRevlogEntry],
) -> List[CardStatsResponse.StatsRevlogEntry]:
    return list(filter(lambda x: x.review_kind != REVLOG_CRAM or x.ease != 0, revlogs))


def get_last_review_date(card: Card):
    revlogs = mw.col.card_stats_data(card.id).revlog
    try:
        last_revlog = list(filter(lambda x: x.button_chosen >= 1, revlogs))[0]
        last_review_date = (
            math.ceil((last_revlog.time - mw.col.sched.day_cutoff) / 86400)
            + mw.col.sched.today
        )
    except IndexError:
        due = card.odue if card.odid else card.due
        last_review_date = due - card.ivl
    return last_review_date


def update_card_due_ivl(card: Card, new_ivl: int):
    # Don't change ivl, it leads to ever-increasing ivl when reschedule is applied repeatedly
    # card.ivl = new_ivl
    last_review_date = get_last_review_date(card)
    if card.odid:
        card.odue = max(last_review_date + new_ivl, 1)
    else:
        card.due = last_review_date + new_ivl
    return card


def get_fuzz_range(interval, elapsed_days):
    min_ivl = max(2, int(round(interval * 0.95 - 1)))
    max_ivl = int(round(interval * 1.05 + 1))
    if interval > elapsed_days:
        min_ivl = max(min_ivl, elapsed_days + 1)
    return min_ivl, max_ivl


def due_to_date(due: int) -> str:
    offset = due - mw.col.sched.today
    today_date = datetime.today()
    return (today_date + timedelta(days=offset)).strftime("%Y-%m-%d")
