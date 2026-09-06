"""What posting degrades to when no host can be elected.

A host lives inside whichever addon won the election, and that addon can be
disabled, or torn down, or simply not shipped with the toast UI yet. None of
that may turn a sync summary into a traceback, so every path out of post()
ends here: Anki's own tooltip, which is exactly the behaviour this package
replaces. Worse than a stack, but never worse than today.

The one thing kept over plain tooltip() calls is a small cache of what was
posted, so that an update() against a fallback handle can re-render the merged
entry rather than showing a fragment of it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .spec import HOST_DEFAULT, STICKY, merge_spec

# Handles minted here are prefixed so that update() and dismiss() can route
# them back to the fallback even if a host has been elected in the meantime.
PREFIX = "fb:"

DEFAULT_PERIOD_MS = 7000

# tooltip() has no sticky mode, so STICKY becomes "long enough to read twice".
STICKY_PERIOD_MS = 30000

# Anki's tooltip sits 100px up by default; lifting it clears the answer buttons.
Y_OFFSET = 200

_recent: Dict[str, Dict[str, Any]] = {}
_counter = 0


def render(spec: Dict[str, Any]) -> str:
    """Flatten an entry into the one blob of rich text a tooltip can show."""
    parts = []
    source = spec.get("source", "")
    title = spec.get("title", "")
    if source and title:
        parts.append(f"<b>{source}:</b> {title}")
    elif source or title:
        parts.append(f"<b>{source or title}</b>")
    body = spec.get("body", "")
    if body:
        parts.append(body)
    return "<br>".join(parts)


def period_ms(spec: Dict[str, Any]) -> int:
    requested = spec.get("timeout_ms", HOST_DEFAULT)
    if requested == STICKY:
        return STICKY_PERIOD_MS
    if requested == HOST_DEFAULT:
        return DEFAULT_PERIOD_MS
    return requested


def _show(spec: Dict[str, Any]) -> None:
    """Show the entry once no progress dialog is in the way.

    Posting the instant an operation finishes gets the tooltip closed again by
    the progress dialog still tearing down -- the addons each carried their own
    single_shot() workaround for this. mw.progress.single_shot refuses to fire
    under a progress window and retries shortly after, so doing it here once
    means no caller has to.

    aqt is imported inside the function so the package stays importable without
    a running Anki: the tests stub aqt, which has no importable aqt.utils.
    """
    import aqt

    def render_now() -> None:
        from aqt.utils import tooltip

        tooltip(
            render(spec),
            parent=aqt.mw,
            period=period_ms(spec),
            y_offset=Y_OFFSET,
        )

    aqt.mw.progress.single_shot(10, render_now, requires_collection=False)


def post(spec: Dict[str, Any]) -> str:
    global _counter
    _counter += 1
    handle = f"{PREFIX}{_counter}"
    _recent[handle] = spec
    _show(spec)
    return handle


def update(handle: str, fields: Dict[str, Any]) -> bool:
    spec = _recent.get(handle)
    if spec is None:
        return False
    merged = merge_spec(spec, fields)
    _recent[handle] = merged
    _show(merged)
    return True


def dismiss(handle: str) -> bool:
    if _recent.pop(handle, None) is None:
        return False
    from aqt.utils import closeTooltip

    # There is only ever one tooltip, so dismissing any entry closes whatever
    # is on screen. Harmless: it was about to time out anyway.
    closeTooltip()
    return True


def owns(handle: Optional[str]) -> bool:
    return bool(handle) and handle.startswith(PREFIX)
