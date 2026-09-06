"""Post a notification that stacks instead of replacing the last one.

    from ..shared.notify import post

    post(
        source="Copy Anywhere",
        title="Copied 12 fields",
        body=details_html,
        group="sync",
        key="sync",
    )

Every addon vendoring this package registers itself as a candidate host when
it is imported; the first post elects one of them and everybody's entries go
into that one window. Nothing here needs to know which addons are installed,
or whether any others are: with one addon it hosts itself, and with none of
them able to host, posts degrade to Anki's tooltip.

Calls into the elected host are wrapped, because the host belongs to a
different addon than the caller and can be disabled or torn down underneath
it. A host that raises is retired, the runner-up takes over, and the post is
retried; when no candidate is left the post falls back. Nothing raises out of
this module -- it runs inside sync_did_finish, where an exception surfaces to
the user as a broken sync.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, Tuple

from . import fallback, registry
from .spec import HOST_DEFAULT, LEVELS, STICKY, make_spec

__all__ = ["post", "update", "dismiss", "HOST_DEFAULT", "STICKY", "LEVELS"]

_MISSING = object()


def _anchor() -> Optional[Any]:
    """The object every vendored copy shares. None before Anki has a window."""
    import aqt

    return getattr(aqt, "mw", None)


def _make_host() -> Optional[Any]:
    """This copy's offer to host, or None if it cannot.

    host.py is the Qt half of the package; while it is absent every copy
    declines, the registry runs out of candidates, and posts fall back.
    """
    try:
        from .host import ToastHost
    except ImportError:
        return None
    try:
        return ToastHost()
    except Exception:
        return None


def _via_host(method: str, *args: Any) -> Any:
    """Run `method` on the elected host, retiring hosts that raise.

    Returns _MISSING when there is no host left to try, which is the caller's
    signal to fall back. The loop terminates because retire() removes a
    candidate every time round.
    """
    anchor = _anchor()
    if anchor is None:
        return _MISSING
    while True:
        host = registry.host(anchor)
        if host is None:
            return _MISSING
        try:
            return getattr(host, method)(*args)
        except Exception:
            ident = registry.current_host_ident(anchor)
            if ident is None:
                return _MISSING
            registry.retire(anchor, ident)


def post(
    source: str,
    title: str,
    body: str = "",
    level: str = "info",
    timeout_ms: int = HOST_DEFAULT,
    group: str = "",
    key: str = "",
    actions: Sequence[Tuple[str, Callable[[], None]]] = (),
    **extra: Any,
) -> str:
    """Show an entry and return its handle. See spec.make_spec for the fields.

    Reposting under the same `key` replaces that entry in place rather than
    adding a second one, which is how a two-stage operation reports progress
    without leaving its first message behind.
    """
    spec = make_spec(
        source=source,
        title=title,
        body=body,
        level=level,
        timeout_ms=timeout_ms,
        group=group,
        key=key,
        actions=actions,
        **extra,
    )
    handle = _via_host("post", spec)
    if handle is _MISSING:
        return fallback.post(spec)
    return handle


def update(handle: str, **fields: Any) -> bool:
    """Change a live entry. Unstated fields keep their current values."""
    if fallback.owns(handle):
        return fallback.update(handle, fields)
    result = _via_host("update", handle, fields)
    return False if result is _MISSING else bool(result)


def dismiss(handle: str) -> bool:
    """Take an entry down early."""
    if fallback.owns(handle):
        return fallback.dismiss(handle)
    result = _via_host("dismiss", handle)
    return False if result is _MISSING else bool(result)


# Registration happens at import, not on first use: every copy has to be on the
# ballot before anybody counts the votes. Anki imports all addons in loadAddons()
# before any of them can post, so by the time _via_host runs the field is
# complete. A failure here (no mw yet) leaves this copy off the ballot rather
# than breaking the addon that imported it.
try:
    _a = _anchor()
    if _a is not None:
        registry.register(_a, __name__, _make_host)
    del _a
except Exception:
    pass
