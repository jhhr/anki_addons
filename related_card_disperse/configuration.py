from __future__ import annotations

import uuid
from typing import Optional, TypedDict

from aqt import mw

from .core import join_quoted_names, split_quoted_names
from .shared.interpolate.interpolate_fields import NOTE_ID, intr_format

tag = mw.addonManager.addonFromModule(__name__)

# What a new rule starts with: the reviewed note's own cards, i.e. plain
# sibling dispersal. Anything is better than an empty query, which
# find_cards reads as the whole collection.
DEFAULT_RELATED_CARD_QUERY = "nid:" + intr_format(NOTE_ID)

# anki.consts.MODEL_CLOZE, spelled out so the note type checks below stay
# importable -- and testable -- without a running Anki.
CLOZE_NOTE_TYPE = 1

# Derived rules are worked out from the collection's note types whenever they
# are needed and never written to the config. The prefix keeps their guids
# clear of stored rules' uuid4s wherever a run is keyed by guid.
DERIVED_RULE_GUID_PREFIX = "__default_siblings__:"


class RelatedRule(TypedDict):
    guid: str
    name: str
    enabled: bool
    target_note_types: str
    target_card_types: str
    related_card_query: str
    use_code: bool
    query_code: str
    on_review: bool
    on_sync: bool
    max_related_cards: Optional[int]


class ConfigData(TypedDict):
    version: str
    default_max_related_cards: int
    bury_min_gap: int
    hide_review_report: bool
    hide_review_details: bool
    hide_review_unchanged: bool
    dedupe_sync_groups: bool
    disperse_siblings_default: bool
    disperse_across_decks: bool
    rules: list[RelatedRule]


def load_config() -> ConfigData:
    return mw.addonManager.getConfig(tag)


def save_config(data: ConfigData) -> None:
    mw.addonManager.writeConfig(tag, data)


def _normalize_name_list(value: object) -> str:
    """Accept either the stored `"A", "B"` string or a plain list of names."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return join_quoted_names([str(v) for v in value if str(v).strip()])
    return ""


def default_rule() -> RelatedRule:
    return RelatedRule(
        guid=str(uuid.uuid4()),
        name="",
        enabled=True,
        target_note_types="",
        target_card_types="",
        related_card_query=DEFAULT_RELATED_CARD_QUERY,
        use_code=False,
        query_code="",
        on_review=True,
        on_sync=True,
        max_related_cards=None,
    )


def is_derived_rule(rule: RelatedRule) -> bool:
    """Whether a rule came from the default sibling toggle rather than the config."""
    return str(rule.get("guid", "")).startswith(DERIVED_RULE_GUID_PREFIX)


def note_type_has_siblings(model: dict) -> bool:
    """Whether a note type makes more than one card per note, and so has siblings.

    A cloze note type qualifies whatever its template count: its cards come from
    the cloze numbers in the fields, not from templates.
    """
    if model.get("type") == CLOZE_NOTE_TYPE:
        return True
    return len(model.get("tmpls") or []) > 1


def targeted_note_type_names(rules: list[RelatedRule]) -> set[str]:
    """Every note type some stored rule names, whether that rule is enabled or not.

    A disabled rule counts, and that is the point: saving a disabled rule for a
    note type is how the default sibling dispersal is turned off for it.
    """
    names: set[str] = set()
    for rule in rules:
        names.update(split_quoted_names(rule.get("target_note_types", "")))
    return names


def derived_sibling_rule(model: dict) -> RelatedRule:
    """The default rule for one note type: disperse the reviewed note's own cards."""
    name = str(model.get("name", ""))
    rule = default_rule()
    rule["guid"] = DERIVED_RULE_GUID_PREFIX + str(model.get("id", name))
    rule["name"] = name
    rule["target_note_types"] = join_quoted_names([name])
    return rule


def derived_sibling_rules(rules: list[RelatedRule], models: list[dict]) -> list[RelatedRule]:
    """Default sibling rules for every note type no stored rule speaks for."""
    targeted = targeted_note_type_names(rules)
    return [
        derived_sibling_rule(model)
        for model in sorted(models, key=lambda m: str(m.get("name", "")).lower())
        if str(model.get("name", "")) not in targeted and note_type_has_siblings(model)
    ]


def migrate_data(data: dict) -> ConfigData:
    data.setdefault("version", "1.0.0")
    data.setdefault("default_max_related_cards", 20)
    # Renamed: the flag never had anything to do with overlap. It gates
    # reporting of runs that rescheduled nothing.
    if "show_unchanged_outcome" not in data and "show_no_overlap_outcome" in data:
        data["show_unchanged_outcome"] = data["show_no_overlap_outcome"]
    data.pop("show_no_overlap_outcome", None)
    # Migrated to hide_review_unchanged (inverted): True = show → False = don't hide.
    if "show_unchanged_outcome" in data and "hide_review_unchanged" not in data:
        data["hide_review_unchanged"] = not bool(data["show_unchanged_outcome"])
    data.pop("show_unchanged_outcome", None)
    data.setdefault("hide_review_report", False)
    data.setdefault("hide_review_details", False)
    data.setdefault("hide_review_unchanged", False)
    data.setdefault("dedupe_sync_groups", True)
    data.setdefault("disperse_siblings_default", False)
    data.setdefault("disperse_across_decks", True)
    data.setdefault("bury_min_gap", 0)
    rules = data.get("rules", []) or []

    normalized_rules: list[RelatedRule] = []
    for maybe_rule in rules:
        if not isinstance(maybe_rule, dict):
            continue
        base = default_rule()
        base["guid"] = str(maybe_rule.get("guid") or uuid.uuid4())
        base["name"] = str(maybe_rule.get("name") or "")
        base["enabled"] = bool(maybe_rule.get("enabled", True))
        base["target_note_types"] = _normalize_name_list(maybe_rule.get("target_note_types"))
        # Absent means "every card type of the targeted note types", which is
        # what rules written before card type targeting existed did.
        base["target_card_types"] = _normalize_name_list(maybe_rule.get("target_card_types"))
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
    data["hide_review_report"] = bool(data["hide_review_report"])
    data["hide_review_details"] = bool(data["hide_review_details"])
    data["hide_review_unchanged"] = bool(data["hide_review_unchanged"])
    data["dedupe_sync_groups"] = bool(data["dedupe_sync_groups"])
    data["disperse_siblings_default"] = bool(data["disperse_siblings_default"])
    data["disperse_across_decks"] = bool(data["disperse_across_decks"])
    try:
        data["bury_min_gap"] = max(0, int(data["bury_min_gap"]))
    except (TypeError, ValueError):
        data["bury_min_gap"] = 0
    return data  # type: ignore[return-value]


class Config:
    def __init__(self) -> None:
        self.data: ConfigData = {
            "version": "1.0.0",
            "default_max_related_cards": 20,
            "bury_min_gap": 0,
            "hide_review_report": False,
            "hide_review_details": False,
            "hide_review_unchanged": False,
            "dedupe_sync_groups": True,
            "disperse_siblings_default": False,
            "disperse_across_decks": True,
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
    def hide_review_report(self) -> bool:
        return self.data["hide_review_report"]

    @property
    def hide_review_details(self) -> bool:
        return self.data["hide_review_details"]

    @property
    def hide_review_unchanged(self) -> bool:
        return self.data["hide_review_unchanged"]

    @property
    def dedupe_sync_groups(self) -> bool:
        return self.data["dedupe_sync_groups"]

    @property
    def disperse_siblings_default(self) -> bool:
        """Whether note types with no rule of their own get sibling dispersal anyway.

        The replacement for the old "Auto-disperse siblings" setting: on, every
        multi-card note type nobody wrote a rule for behaves as if it had one
        dispersing the reviewed note's own cards.
        """
        return self.data["disperse_siblings_default"]

    def rules_for_model(self, model: Optional[dict]) -> list[RelatedRule]:
        """The rules in play for one note type, derived one included.

        Derived per note type rather than for the collection at large: this runs
        once per reviewed card, and the note type in hand is the only one whose
        default could possibly apply.
        """
        if not model or not self.disperse_siblings_default:
            return self.rules
        if not note_type_has_siblings(model):
            return self.rules
        if str(model.get("name", "")) in targeted_note_type_names(self.rules):
            return self.rules
        return [*self.rules, derived_sibling_rule(model)]

    def has_any_rules(self) -> bool:
        """Whether anything could run at all, stored rule or default sibling one."""
        return bool(self.rules) or self.disperse_siblings_default

    @property
    def bury_min_gap(self) -> int:
        """How close two related cards may come before one is buried.

        Counted in cards of the session, and 0 means the whole session -- one
        card per related group per day, which is what Anki's own sibling burying
        does. A larger session is where a gap earns its keep: two related cards
        eighty cards apart in a hundred-card sitting are not really colliding.
        """
        return self.data["bury_min_gap"]

    @property
    def disperse_across_decks(self) -> bool:
        """Whether "Disperse due cards" looks at the whole day or one deck tree.

        A rule query is not deck-scoped, so relations routinely point from one
        top-level deck into another -- and a session gathered from a single tree
        resolves those relations only to throw them away, reporting no
        collisions on a session full of them. On by default because the review
        hook has always buried across decks; off restores the old behaviour for
        anyone who wants the command to touch only the deck they named.
        """
        return self.data["disperse_across_decks"]

    def update_global(
        self,
        *,
        default_cap: int,
        bury_min_gap: int,
        hide_review_report: bool,
        hide_review_details: bool,
        hide_review_unchanged: bool,
        dedupe_sync: bool,
        disperse_siblings_default: bool,
        disperse_across_decks: bool,
    ) -> None:
        self.data["default_max_related_cards"] = max(1, int(default_cap))
        self.data["bury_min_gap"] = max(0, int(bury_min_gap))
        self.data["hide_review_report"] = bool(hide_review_report)
        self.data["hide_review_details"] = bool(hide_review_details)
        self.data["hide_review_unchanged"] = bool(hide_review_unchanged)
        self.data["dedupe_sync_groups"] = bool(dedupe_sync)
        self.data["disperse_across_decks"] = bool(disperse_across_decks)
        self.data["disperse_siblings_default"] = bool(disperse_siblings_default)
        self.save()

    def replace_rules(self, rules: list[RelatedRule]) -> None:
        # A derived rule is worked out from the note types every time it is
        # needed; storing one would freeze it, and shadow that note type's
        # default for good.
        stored = [rule for rule in rules if not is_derived_rule(rule)]
        self.data["rules"] = migrate_data({"rules": stored}).get("rules", [])
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
