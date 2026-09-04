from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from anki.cards import Card
from anki.consts import CARD_TYPE_REV
from anki.stats import REVLOG_CRAM
from anki.stats_pb2 import CardStatsResponse
from anki.utils import ids2str
from aqt import mw

from .configuration import Config, RelatedRule
from .core import (
    cap_card_ids,
    dedupe_preserve_order,
    group_overlapping_sets,
    normalize_card_id_result,
    split_note_type_names,
    summarize_outcome,
)
from .shared.anki.write_custom_data import write_custom_data
from .shared.interpolate.execute_code import execute_code_core
from .shared.interpolate.interpolate_fields import interpolate_from_text
from .shared.scheduling.due_dates import due_to_date, get_fuzz_range


@dataclass
class QueryResolution:
    raw_count: int
    filtered_count: int
    dropped_trigger_count: int
    capped_count: int
    card_ids: list[int]
    raw_related_ids: set[int]
    error: Optional[str] = None


@dataclass
class DispersePlan:
    card_ids: list[int]
    due_ranges: dict[int, tuple[int, int]]
    current_dues: dict[int, int]
    last_reviews: dict[int, int]
    best_due_dates: dict[int, int]
    min_gap: int


@dataclass
class RuleOutcome:
    message: str
    updated: int


StatsCache = dict[int, CardStatsResponse]


def _rule_name(rule: RelatedRule) -> str:
    return rule.get("name") or f"Rule {rule.get('guid', '')[:8]}"


def _rule_cap(rule: RelatedRule, config: Config) -> int:
    specific = rule.get("max_related_cards")
    if isinstance(specific, int) and specific > 0:
        return specific
    return config.default_max_related_cards


def get_applicable_rules(
    rules: list[RelatedRule],
    note_type_name: str,
    *,
    on_review: bool = False,
    on_sync: bool = False,
) -> list[RelatedRule]:
    result: list[RelatedRule] = []
    for rule in rules:
        if on_review and not rule.get("on_review", True):
            continue
        if on_sync and not rule.get("on_sync", True):
            continue
        targets = split_note_type_names(rule.get("target_note_types", ""))
        if note_type_name in targets:
            result.append(rule)
    return result


def _as_query_or_ids(
    rule: RelatedRule,
    reviewed_card: Card,
) -> tuple[Optional[str], list[int], Optional[str]]:
    note = reviewed_card.note()
    if rule.get("use_code", False):
        code = rule.get("query_code", "")
        interpolated_code, invalid_fields = interpolate_from_text(code, source_note=note)
        if invalid_fields:
            return None, [], f"invalid fields in code: {', '.join(invalid_fields)}"
        if interpolated_code is None:
            return None, [], "could not interpolate query code"
        result, error = execute_code_core(
            interpolated_code,
            note,
            extra_globals={"reviewed_card": reviewed_card, "reviewed_note": note},
        )
        if error:
            return None, [], error
        if isinstance(result, str):
            return result, [], None
        try:
            return None, normalize_card_id_result(result), None
        except ValueError as exc:
            return None, [], str(exc)

    query_template = rule.get("related_card_query", "")
    query, invalid_fields = interpolate_from_text(query_template, source_note=note)
    if invalid_fields:
        return None, [], f"invalid fields in query: {', '.join(invalid_fields)}"
    if query is None:
        return None, [], "could not interpolate query"
    return query, [], None


def _post_filter_review_cards(card_ids: list[int]) -> list[int]:
    if not card_ids:
        return []
    rows = mw.col.db.all(
        f"""
        SELECT id, type, queue
        FROM cards
        WHERE id IN {ids2str(card_ids)}
        """
    )
    allowed = [cid for cid, ctype, queue in rows if ctype == CARD_TYPE_REV and queue != -1]
    return dedupe_preserve_order(allowed)


def resolve_rule_candidates(
    rule: RelatedRule,
    reviewed_card: Card,
    config: Config,
    *,
    drop_trigger_card: bool,
    apply_cap: bool = True,
) -> QueryResolution:
    """Resolve one rule's related cards for a reviewed card.

    When ``apply_cap`` is False, ``card_ids`` is the full filtered list and
    ``capped_count`` is always 0.
    """
    query, direct_ids, error = _as_query_or_ids(rule, reviewed_card)
    if error:
        return QueryResolution(0, 0, 0, 0, [], set(), error=error)

    if query is not None:
        found_ids = list(mw.col.find_cards(query))
    else:
        found_ids = direct_ids

    raw_ids = dedupe_preserve_order(found_ids)
    state_filtered = _post_filter_review_cards(raw_ids)
    filtered_count = max(0, len(raw_ids) - len(state_filtered))
    filtered = state_filtered
    if drop_trigger_card:
        filtered = [cid for cid in filtered if cid != reviewed_card.id]
    dropped_trigger_count = max(0, len(state_filtered) - len(filtered))

    if apply_cap:
        capped, capped_count = cap_card_ids(filtered, _rule_cap(rule, config))
    else:
        capped, capped_count = filtered, 0
    return QueryResolution(
        raw_count=len(raw_ids),
        filtered_count=filtered_count,
        dropped_trigger_count=dropped_trigger_count,
        capped_count=capped_count,
        card_ids=capped,
        raw_related_ids=set(filtered),
    )


def _filter_revlogs(
    revlogs: list[CardStatsResponse.StatsRevlogEntry],
) -> list[CardStatsResponse.StatsRevlogEntry]:
    return [x for x in revlogs if x.review_kind != REVLOG_CRAM or x.ease != 0]


def _get_stats(card_id: int, stats_cache: StatsCache) -> CardStatsResponse:
    cached = stats_cache.get(card_id)
    if cached is not None:
        return cached
    stats = mw.col.card_stats_data(card_id)
    stats_cache[card_id] = stats
    return stats


def _last_review_date(card: Card, revlogs: list[CardStatsResponse.StatsRevlogEntry]) -> int:
    for revlog in revlogs:
        if revlog.button_chosen >= 1:
            return math.ceil((revlog.time - mw.col.sched.day_cutoff) / 86400) + mw.col.sched.today
    due = card.odue if card.odid else card.due
    return due - card.ivl


def _get_desired_retention(card: Card) -> float:
    dr = getattr(card, "desired_retention", None)
    if isinstance(dr, (int, float)) and 0 < float(dr) < 1:
        return float(dr)
    deck_id = card.odid if card.odid else card.did
    deck_conf = mw.col.decks.config_dict_for_deck_id(deck_id)
    preset_dr = deck_conf.get("desiredRetention", 0.9)
    if isinstance(preset_dr, (int, float)) and 0 < float(preset_dr) < 1:
        return float(preset_dr)
    return 0.9


def _max_interval(card: Card) -> int:
    deck_id = card.odid if card.odid else card.did
    deck_conf = mw.col.decks.config_dict_for_deck_id(deck_id)
    return int(deck_conf.get("rev", {}).get("maxIvl", 36500))


def _get_due_range(
    card: Card,
    desired_retention: float,
    maximum_interval: int,
    stats_cache: StatsCache,
) -> tuple[tuple[int, int], int]:
    ivl = card.ivl
    due = card.odue if card.odid else card.due

    stats = _get_stats(card.id, stats_cache)
    revlogs = _filter_revlogs(stats.revlog)
    last_review = _last_review_date(card, revlogs)

    new_ivl = int(round(9 * ivl * (1 / desired_retention - 1)))
    new_ivl = min(new_ivl, maximum_interval)

    if new_ivl <= 2:
        return (due, due), last_review

    last_elapsed_days = int((revlogs[0].time - revlogs[1].time) / 86400) if len(revlogs) >= 2 else 0

    min_ivl, max_ivl = get_fuzz_range(new_ivl, last_elapsed_days)

    if due >= mw.col.sched.today:
        due_range = (
            max(last_review + min_ivl, mw.col.sched.today),
            max(last_review + max_ivl, mw.col.sched.today),
        )
    elif last_review + max_ivl > mw.col.sched.today:
        due_range = (mw.col.sched.today, last_review + max_ivl)
    else:
        due_range = (due, due)

    return due_range, last_review


def build_disperse_plan(card_ids: list[int], stats_cache: StatsCache) -> DispersePlan:
    due_ranges: dict[int, tuple[int, int]] = {}
    current_dues: dict[int, int] = {}
    last_reviews: dict[int, int] = {}

    for cid in card_ids:
        card = mw.col.get_card(cid)
        current_dues[cid] = card.odue if card.odid else card.due
        due_range, last_review = _get_due_range(
            card,
            desired_retention=_get_desired_retention(card),
            maximum_interval=_max_interval(card),
            stats_cache=stats_cache,
        )
        due_ranges[cid] = due_range
        last_reviews[cid] = last_review

    min_gap, best_due_dates = maximize_due_gap(due_ranges)
    return DispersePlan(
        card_ids=card_ids,
        due_ranges=due_ranges,
        current_dues=current_dues,
        last_reviews=last_reviews,
        best_due_dates=best_due_dates,
        min_gap=min_gap,
    )


def apply_disperse_plan(plan: DispersePlan, undo_entry: int) -> list[str]:
    messages: list[str] = []
    for cid, due in plan.best_due_dates.items():
        card = mw.col.get_card(cid)
        old_due = card.odue if card.odid else card.due
        adjusted_due = max(due, mw.col.sched.today + 1)
        if card.odid:
            card.odue = max(adjusted_due, 1)
        else:
            card.due = adjusted_due
        write_custom_data(card, "v", "d")
        mw.col.update_card(card)
        mw.col.merge_undo_entries(undo_entry)
        messages.append(
            f"Dispersed card {cid} from {due_to_date(old_due)} to {due_to_date(adjusted_due)}"
        )
    return messages


def _is_noop_plan(plan: DispersePlan) -> bool:
    return plan.best_due_dates == plan.current_dues


def run_rule_for_reviewed_card(
    rule: RelatedRule,
    reviewed_card: Card,
    config: Config,
    stats_cache: StatsCache,
    undo_entry: int,
    processed_rule_card_pairs: set[tuple[str, int]],
) -> RuleOutcome:
    pair_key = (rule["guid"], reviewed_card.id)
    if pair_key in processed_rule_card_pairs:
        return RuleOutcome(
            summarize_outcome(_rule_name(rule), 0, 0, 0, 0, "skipped(already processed)"),
            0,
        )
    processed_rule_card_pairs.add(pair_key)

    query_result = resolve_rule_candidates(rule, reviewed_card, config, drop_trigger_card=True)
    rule_name = _rule_name(rule)
    if query_result.error:
        return RuleOutcome(
            summarize_outcome(rule_name, 0, 0, 0, 0, f"error({query_result.error})"),
            0,
        )

    if len(query_result.card_ids) <= 1:
        return RuleOutcome(
            summarize_outcome(
                rule_name,
                query_result.raw_count,
                query_result.filtered_count + query_result.dropped_trigger_count,
                query_result.capped_count,
                0,
                "skipped(empty or single card)",
            ),
            0,
        )

    plan = build_disperse_plan(query_result.card_ids, stats_cache)
    if _is_noop_plan(plan):
        outcome_text = (
            "skipped(non-overlapping due ranges)"
            if plan.min_gap == 0
            else "skipped(already optimally placed)"
        )
        return RuleOutcome(
            summarize_outcome(
                rule_name,
                query_result.raw_count,
                query_result.filtered_count + query_result.dropped_trigger_count,
                query_result.capped_count,
                0,
                outcome_text,
            ),
            0,
        )

    details = apply_disperse_plan(plan, undo_entry)
    return RuleOutcome(
        summarize_outcome(
            rule_name,
            query_result.raw_count,
            query_result.filtered_count + query_result.dropped_trigger_count,
            query_result.capped_count,
            len(details),
            "dispersed",
        )
        + "<br>"
        + "<br>".join(details),
        len(details),
    )


def run_sync_grouped(
    reviewed_cards: list[Card],
    config: Config,
) -> list[str]:
    messages: list[str] = []
    if not reviewed_cards:
        return messages

    stats_cache: StatsCache = {}
    undo_entry = mw.col.add_custom_undo_entry("Disperse related cards after sync")

    by_rule: dict[str, dict[str, Any]] = {}
    for reviewed_card in reviewed_cards:
        note = reviewed_card.note()
        note_type = note.note_type()
        if not note_type:
            continue
        sync_rules = get_applicable_rules(config.rules, note_type["name"], on_sync=True)
        for rule in sync_rules:
            rule_guid = rule["guid"]
            if rule_guid not in by_rule:
                by_rule[rule_guid] = {"rule": rule, "sets": []}
            resolution = resolve_rule_candidates(
                rule,
                reviewed_card,
                config,
                drop_trigger_card=False,
                apply_cap=False,
            )
            if resolution.error or not resolution.card_ids:
                continue
            by_rule[rule_guid]["sets"].append(set(resolution.card_ids))

    for entry in by_rule.values():
        rule: RelatedRule = entry["rule"]
        sets: list[set[int]] = entry["sets"]
        if not sets:
            continue
        grouped = group_overlapping_sets(sets) if config.dedupe_sync_groups else sets
        for group in grouped:
            group_ids = dedupe_preserve_order(list(group))
            capped_ids, capped_count = cap_card_ids(group_ids, _rule_cap(rule, config))
            if len(capped_ids) <= 1:
                messages.append(
                    summarize_outcome(_rule_name(rule), len(group_ids), 0, capped_count, 0, "skipped(empty or single card)")
                )
                continue
            plan = build_disperse_plan(capped_ids, stats_cache)
            if _is_noop_plan(plan):
                outcome_text = (
                    "skipped(non-overlapping due ranges)"
                    if plan.min_gap == 0
                    else "skipped(already optimally placed)"
                )
                messages.append(
                    summarize_outcome(
                        _rule_name(rule),
                        len(group_ids),
                        0,
                        capped_count,
                        0,
                        outcome_text,
                    )
                )
                continue
            details = apply_disperse_plan(plan, undo_entry)
            messages.append(
                summarize_outcome(
                    _rule_name(rule),
                    len(group_ids),
                    0,
                    capped_count,
                    len(details),
                    "dispersed",
                )
            )

    return messages


def maximize_due_gap(points_dict: Dict[int, Tuple[int, int]]) -> tuple[int, dict[int, int]]:
    """Return the maximum minimum gap and the assigned due date per card id."""
    if not points_dict:
        return 0, {}
    points_list = list(points_dict.items())
    points_list.sort(key=lambda x: x[1][1])

    intervals_only = [interval for _, interval in points_list]
    max_min_gap, initial_arrangement = find_max_min_gap_and_arrangement(intervals_only)

    optimized_arrangement_dict = {
        points_list[i][0]: initial_arrangement[i] for i in range(len(points_list))
    }

    return max_min_gap, optimized_arrangement_dict


def find_max_min_gap_and_arrangement(
    points: list[tuple[int, int]],
) -> tuple[int, list[int]]:
    """Binary-search the largest feasible minimum gap and one valid arrangement."""
    if not points:
        return 0, []
    points.sort(key=lambda x: x[1])
    min_gap = 0
    max_gap = points[-1][1] - points[0][0]
    best_gap = 0
    arrangement = []

    def can_place_points_with_arrangement(points, min_gap):
        last_point_position = points[0][0]
        temp_arrangement = [last_point_position]
        for i in range(1, len(points)):
            next_possible_point = last_point_position + min_gap
            if next_possible_point > points[i][1]:
                return False, []
            last_point_position = max(next_possible_point, points[i][0])
            temp_arrangement.append(last_point_position)
        return True, temp_arrangement

    while min_gap <= max_gap:
        mid_gap = (min_gap + max_gap) // 2
        can_place, temp_arrangement = can_place_points_with_arrangement(points, mid_gap)
        if can_place:
            best_gap = mid_gap
            arrangement = temp_arrangement
            min_gap = mid_gap + 1
        else:
            max_gap = mid_gap - 1

    return best_gap, arrangement
