"""Bookkeeping for the toast stack: what is showing, what is waiting, what
belongs together. Knows nothing about widgets, geometry or timers.

Splitting this out is not tidiness for its own sake. conftest stubs aqt and
anki with MagicMock, so anything importing Qt cannot be meaningfully tested;
keeping the ordering, grouping, dedup and queueing rules here means the parts
most likely to be subtly wrong are the parts under test.

The host drives it in one direction only: mutate, then re-read visible() and
reconcile the widgets against it. There is no change-event protocol to keep in
sync, and no way for the widget tree and the model to disagree about what a
dismissal should have promoted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .spec import HOST_DEFAULT, STICKY, merge_spec

# Anki's own tooltip defaults to 3s, which is too short for a sync summary
# somebody may want to read; the addons currently pass 5-10s by hand.
DEFAULT_TIMEOUT_MS = 7000

# Beyond this, the stack stops being glanceable and starts being a wall.
DEFAULT_MAX_VISIBLE = 4

# addon_config_sync posts from media_sync_did_start_or_stop, which lands
# noticeably after the sync_did_finish posts. Holding a group open past its
# last entry lets those late arrivals join it instead of starting a second one.
DEFAULT_GROUP_WINDOW_MS = 5000


class Stack:
    """The live set of entries, ordered for display.

    Order is oldest-first: groups by when the group started, entries by when
    they were posted within it. The host decides which end of the screen that
    maps onto; the model does not care.
    """

    def __init__(
        self,
        max_visible: int = DEFAULT_MAX_VISIBLE,
        group_window_ms: int = DEFAULT_GROUP_WINDOW_MS,
        default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        self.max_visible = max_visible
        self.group_window_ms = group_window_ms
        self.default_timeout_ms = default_timeout_ms
        self._entries: List[Dict[str, Any]] = []
        self._groups: Dict[str, Dict[str, Any]] = {}
        self._counter = 0
        # Monotonic, never derived from len(_groups): pruning a bucket would
        # otherwise let the next id collide with a surviving one.
        self._group_counter = 0

    # -- posting -----------------------------------------------------------

    def post(self, spec: Dict[str, Any], now_ms: int) -> str:
        """Add an entry, or refresh the one this spec's key already owns.

        Returns the handle either way, so a caller reposting under a key does
        not have to care which happened.
        """
        existing = self._find_by_key(spec)
        if existing is not None:
            existing["spec"] = merge_spec(existing["spec"], spec)
            existing["posted_ms"] = now_ms
            self._touch_group(existing["group_id"], now_ms)
            return existing["handle"]

        self._counter += 1
        handle = f"n{self._counter}"
        group_id = self._assign_group(spec.get("group", ""), now_ms)
        self._entries.append(
            {
                "handle": handle,
                "spec": spec,
                "group_id": group_id,
                "posted_ms": now_ms,
                "pinned": False,
            }
        )
        return handle

    def update(self, handle: str, fields: Dict[str, Any]) -> bool:
        entry = self.entry(handle)
        if entry is None:
            return False
        entry["spec"] = merge_spec(entry["spec"], fields)
        return True

    def dismiss(self, handle: str) -> bool:
        entry = self.entry(handle)
        if entry is None:
            return False
        self._entries.remove(entry)
        self._prune_groups()
        return True

    def clear(self) -> None:
        self._entries.clear()
        self._groups.clear()

    # -- reading -----------------------------------------------------------

    def entry(self, handle: str) -> Optional[Dict[str, Any]]:
        for entry in self._entries:
            if entry["handle"] == handle:
                return entry
        return None

    def ordered(self) -> List[Dict[str, Any]]:
        """Every live entry, oldest group first, oldest entry within it first."""
        return sorted(
            self._entries,
            key=lambda e: (self._groups[e["group_id"]]["first_ms"], e["posted_ms"]),
        )

    def visible(self) -> List[Dict[str, Any]]:
        """The entries the host should have on screen right now.

        Pinned entries hold their slot: the user asked for them to stay, so a
        burst of new posts must not push them into the queue behind their back.
        """
        ordered = self.ordered()
        shown = {e["handle"] for e in ordered if e["pinned"]}
        room = max(self.max_visible - len(shown), 0)
        for entry in ordered:
            if room == 0:
                break
            if not entry["pinned"]:
                shown.add(entry["handle"])
                room -= 1
        return [e for e in ordered if e["handle"] in shown]

    def queued(self) -> List[Dict[str, Any]]:
        shown = {e["handle"] for e in self.visible()}
        return [e for e in self.ordered() if e["handle"] not in shown]

    def group_name(self, entry: Dict[str, Any]) -> str:
        return self._groups[entry["group_id"]]["name"]

    def group_members(self, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The visible entries sharing this entry's group, for header rendering."""
        return [e for e in self.visible() if e["group_id"] == entry["group_id"]]

    # -- timing ------------------------------------------------------------

    def timeout_ms(self, entry: Dict[str, Any]) -> Optional[int]:
        """How long this entry should live, or None if it should not expire."""
        if entry["pinned"]:
            return None
        requested = entry["spec"].get("timeout_ms", HOST_DEFAULT)
        if requested == STICKY:
            return None
        if requested == HOST_DEFAULT:
            return self.default_timeout_ms
        return requested

    def set_pinned(self, handle: str, pinned: bool) -> bool:
        entry = self.entry(handle)
        if entry is None:
            return False
        entry["pinned"] = pinned
        return True

    # -- grouping ----------------------------------------------------------

    def _assign_group(self, name: str, now_ms: int) -> str:
        """Join the newest open bucket of this name, or open a fresh one.

        An empty group name still gets a bucket, a private one, so that
        ungrouped entries order alongside grouped ones without a special case.
        """
        if name:
            open_buckets = [
                g
                for g in self._groups.values()
                if g["name"] == name and now_ms - g["last_ms"] <= self.group_window_ms
            ]
            if open_buckets:
                bucket = max(open_buckets, key=lambda g: g["last_ms"])
                bucket["last_ms"] = now_ms
                return bucket["id"]

        self._group_counter += 1
        group_id = f"g{self._group_counter}"
        self._groups[group_id] = {
            "id": group_id,
            "name": name,
            "first_ms": now_ms,
            "last_ms": now_ms,
        }
        return group_id

    def _touch_group(self, group_id: str, now_ms: int) -> None:
        bucket = self._groups.get(group_id)
        if bucket is not None:
            bucket["last_ms"] = now_ms

    def _prune_groups(self) -> None:
        live = {e["group_id"] for e in self._entries}
        for group_id in [g for g in self._groups if g not in live]:
            del self._groups[group_id]

    # -- dedup -------------------------------------------------------------

    def _find_by_key(self, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Keys are scoped to their source, so two addons may both use "sync"."""
        key = spec.get("key", "")
        if not key:
            return None
        source = spec.get("source", "")
        for entry in self._entries:
            if entry["spec"].get("key") == key and entry["spec"].get("source") == source:
                return entry
        return None
