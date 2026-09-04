from __future__ import annotations

import uuid
from typing import Optional, TypedDict

from aqt import mw

from .shared.interpolate.interpolate_fields import NOTE_ID, intr_format


tag = mw.addonManager.addonFromModule(__name__)

# What a new rule starts with: the reviewed note's own cards, i.e. plain
# sibling dispersal. Anything is better than an empty query, which
# find_cards reads as the whole collection.
DEFAULT_RELATED_CARD_QUERY = "nid:" + intr_format(NOTE_ID)


class RelatedRule(TypedDict):
    guid: str
    name: str
    enabled: bool
    target_note_types: str
    related_card_query: str
    use_code: bool
    query_code: str
    on_review: bool
    on_sync: bool
    max_related_cards: Optional[int]


class ConfigData(TypedDict):
    version: str
    default_max_related_cards: int
    show_no_overlap_outcome: bool
    dedupe_sync_groups: bool
    rules: list[RelatedRule]


def load_config() -> ConfigData:
    return mw.addonManager.getConfig(tag)


def save_config(data: ConfigData) -> None:
    mw.addonManager.writeConfig(tag, data)


def _normalize_note_types(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        names = [str(v) for v in value if str(v).strip()]
        return '"' + '", "'.join(names) + '"' if names else ""
    return ""


def default_rule() -> RelatedRule:
    return RelatedRule(
        guid=str(uuid.uuid4()),
        name="",
        enabled=True,
        target_note_types="",
        related_card_query=DEFAULT_RELATED_CARD_QUERY,
        use_code=False,
        query_code="",
        on_review=True,
        on_sync=True,
        max_related_cards=None,
    )


def migrate_data(data: dict) -> ConfigData:
    data.setdefault("version", "1.0.0")
    data.setdefault("default_max_related_cards", 20)
    data.setdefault("show_no_overlap_outcome", True)
    data.setdefault("dedupe_sync_groups", True)
    rules = data.get("rules", []) or []

    normalized_rules: list[RelatedRule] = []
    for maybe_rule in rules:
        if not isinstance(maybe_rule, dict):
            continue
        base = default_rule()
        base["guid"] = str(maybe_rule.get("guid") or uuid.uuid4())
        base["name"] = str(maybe_rule.get("name") or "")
        base["enabled"] = bool(maybe_rule.get("enabled", True))
        base["target_note_types"] = _normalize_note_types(
            maybe_rule.get("target_note_types")
        )
        base["related_card_query"] = str(maybe_rule.get("related_card_query") or "")
        base["use_code"] = bool(maybe_rule.get("use_code", False))
        base["query_code"] = str(maybe_rule.get("query_code") or "")
        base["on_review"] = bool(maybe_rule.get("on_review", True))
        base["on_sync"] = bool(maybe_rule.get("on_sync", True))
        max_related = maybe_rule.get("max_related_cards", None)
        if max_related in ("", None):
            base["max_related_cards"] = None
        else:
            try:
                as_int = int(max_related)
                base["max_related_cards"] = as_int if as_int > 0 else None
            except (TypeError, ValueError):
                base["max_related_cards"] = None
        # A rule with nothing to run cannot be left enabled: an empty query
        # reaches find_cards(""), which matches the entire collection.
        active_query = base["query_code"] if base["use_code"] else base["related_card_query"]
        if not active_query.strip():
            base["enabled"] = False
        normalized_rules.append(base)

    data["rules"] = normalized_rules
    data["default_max_related_cards"] = max(1, int(data["default_max_related_cards"]))
    data["show_no_overlap_outcome"] = bool(data["show_no_overlap_outcome"])
    data["dedupe_sync_groups"] = bool(data["dedupe_sync_groups"])
    return data  # type: ignore[return-value]


class Config:
    def __init__(self) -> None:
        self.data: ConfigData = {
            "version": "1.0.0",
            "default_max_related_cards": 20,
            "show_no_overlap_outcome": True,
            "dedupe_sync_groups": True,
            "rules": [],
        }

    def load(self) -> None:
        self.data = migrate_data(load_config())

    def save(self) -> None:
        save_config(self.data)

    @property
    def rules(self) -> list[RelatedRule]:
        return self.data["rules"]

    @property
    def default_max_related_cards(self) -> int:
        return self.data["default_max_related_cards"]

    @property
    def show_no_overlap_outcome(self) -> bool:
        return self.data["show_no_overlap_outcome"]

    @property
    def dedupe_sync_groups(self) -> bool:
        return self.data["dedupe_sync_groups"]

    def update_global(self, *, default_cap: int, show_no_overlap: bool, dedupe_sync: bool) -> None:
        self.data["default_max_related_cards"] = max(1, int(default_cap))
        self.data["show_no_overlap_outcome"] = bool(show_no_overlap)
        self.data["dedupe_sync_groups"] = bool(dedupe_sync)
        self.save()

    def replace_rules(self, rules: list[RelatedRule]) -> None:
        self.data["rules"] = migrate_data({"rules": rules}).get("rules", [])
        self.save()

    def add_rule(self, rule: RelatedRule) -> None:
        merged = default_rule()
        merged.update(rule)
        if not merged.get("guid"):
            merged["guid"] = str(uuid.uuid4())
        self.data["rules"].append(merged)
        self.save()

    def update_rule(self, guid: str, rule: RelatedRule) -> bool:
        for i, existing in enumerate(self.data["rules"]):
            if existing["guid"] == guid:
                merged = default_rule()
                merged.update(rule)
                merged["guid"] = guid
                self.data["rules"][i] = merged
                self.save()
                return True
        return False

    def remove_rule(self, guid: str) -> bool:
        before = len(self.data["rules"])
        self.data["rules"] = [r for r in self.data["rules"] if r["guid"] != guid]
        changed = len(self.data["rules"]) != before
        if changed:
            self.save()
        return changed

    def reorder_rule(self, from_index: int, to_index: int) -> None:
        if from_index == to_index:
            return
        rules = self.data["rules"]
        if from_index < 0 or to_index < 0 or from_index >= len(rules) or to_index >= len(rules):
            return
        rule = rules.pop(from_index)
        rules.insert(to_index, rule)
        self.save()
