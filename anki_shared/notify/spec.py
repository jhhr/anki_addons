"""The payload one addon hands to another addon's host.

Everything here is plain data -- str, int, tuple, dict, and bare callables --
because a spec is built by one vendored copy of this package and consumed by
another. Custom classes would survive the trip in the sense that attribute
access still works, but they invite isinstance() checks that are False across
copies, and they break the moment two addons ship different versions of the
class. A dict cannot drift.

The other half of that contract is tolerance in both directions: unknown keys
from a newer client are kept aside rather than rejected, and bad values are
corrected rather than raised on. This code runs inside sync_did_finish, where
an exception would surface to the user as a broken sync rather than a missing
toast.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Sequence, Tuple

LEVELS = ("info", "success", "warning", "error")
DEFAULT_LEVEL = "info"

# timeout_ms sentinels
HOST_DEFAULT = 0
STICKY = -1

Action = Tuple[str, Callable[[], None]]


def _clean_actions(actions: Any) -> Tuple[Action, ...]:
    """Keep the (label, callable) pairs and quietly drop anything malformed."""
    if not isinstance(actions, Iterable) or isinstance(actions, (str, bytes)):
        return ()
    cleaned = []
    for item in actions:
        try:
            label, callback = item
        except (TypeError, ValueError):
            continue
        if isinstance(label, str) and label and callable(callback):
            cleaned.append((label, callback))
    return tuple(cleaned)


def _clean_timeout(timeout_ms: Any) -> int:
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
        return HOST_DEFAULT
    if timeout_ms < 0:
        return STICKY
    return timeout_ms


def make_spec(
    source: str,
    title: str,
    body: str = "",
    level: str = DEFAULT_LEVEL,
    timeout_ms: int = HOST_DEFAULT,
    group: str = "",
    key: str = "",
    actions: Sequence[Action] = (),
    **extra: Any,
) -> Dict[str, Any]:
    """Normalise a post into the dict the host stores and renders.

    :param source: addon display name, shown as the entry's origin
    :param title: one-line summary, always visible
    :param body: rich text detail, collapsed behind the entry's disclosure
    :param level: one of LEVELS; anything else degrades to "info"
    :param timeout_ms: HOST_DEFAULT for the host's own default, STICKY to stay
        until dismissed, otherwise a duration
    :param group: entries sharing a non-empty group render under one header
    :param key: stable id within a source; reposting the same key replaces the
        existing entry in place instead of adding another
    :param actions: (label, callback) pairs rendered as buttons
    :param extra: unknown keys from a newer client. Kept, never interpreted, so
        that a client can outrun the elected host without breaking it.
    """
    return {
        "source": str(source or ""),
        "title": str(title or ""),
        "body": str(body or ""),
        "level": level if level in LEVELS else DEFAULT_LEVEL,
        "timeout_ms": _clean_timeout(timeout_ms),
        "group": str(group or ""),
        "key": str(key or ""),
        "actions": _clean_actions(actions),
        "extra": dict(extra),
    }


def merge_spec(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """The spec an update() leaves behind: `new`'s stated fields over `old`.

    Only used for keyed reposts and explicit updates, where the caller may be
    refreshing a title while leaving the body it posted earlier alone.
    """
    merged = dict(old)
    merged.update({k: v for k, v in new.items() if k != "extra"})
    merged["extra"] = {**old.get("extra", {}), **new.get("extra", {})}
    return merged
