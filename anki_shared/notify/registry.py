"""Rendezvous between the vendored copies of this package.

build.py copies anki_shared/notify into every addon that declares it, so
``<addon>.shared.notify.registry`` is a *different module object* per addon:
three addons means three sets of module globals. A stack kept in a module
global -- or in a class attribute, which is how the off-the-shelf toast
libraries do it -- would therefore be three independent stacks drawing over
each other, which is the bug we are trying to remove, not a fix for it.

What the copies do share is one long-lived object: ``aqt.mw``. The registry
hangs off that, and each copy registers itself there when it is imported.

Registration and election are deliberately separate:

    register()  runs at import. Pure, touches no Qt, and assumes nothing
                about which other copies have loaded yet.
    host()      runs on the first post, by which point
                AddonManager.loadAddons() has imported every addon.

That split is what makes the winner independent of load order. Anki sorts
addon directories alphabetically and reverses them when ANKIREVADDONS is set,
so load order is deterministic but arbitrary -- nothing here may depend on it.

This module deliberately does not import aqt: the anchor object is passed in.
That keeps the election unit-testable (conftest stubs aqt with a MagicMock,
against which ``getattr(mw, KEY, None)`` would never return None) and lets the
tests load this file twice to simulate the vendoring.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# Bump when the post()/update()/dismiss() contract changes incompatibly. Copies
# with different PROTOCOL values elect separate hosts rather than crashing: two
# stacks is ugly, a version mismatch that raises inside sync_did_finish is not.
PROTOCOL = 1

# Bump on every behaviour change. The highest IMPL wins the election, so the
# most recently updated addon hosts and the ones you have not touched in a year
# get the new UI for free.
IMPL = 1

# Attribute name on the anchor. Prefixed because mw is shared with Anki itself
# and with every other addon on the user's system.
ANCHOR_ATTR = "_jhhr_notify_registry"

# A candidate is a plain tuple, not a NamedTuple or dataclass, because it is
# written by one copy and read by another: anything that would tempt a reader
# into isinstance() across copies is a trap. isinstance(x, Candidate) is False
# between two copies of this very file, and it fails silently.
Candidate = Tuple[int, str, Callable[[], Any]]


def _registry(anchor: Any) -> Dict[int, Dict[str, Any]]:
    """The per-protocol registry dict, created on first touch."""
    reg = getattr(anchor, ANCHOR_ATTR, None)
    if reg is None:
        reg = {}
        setattr(anchor, ANCHOR_ATTR, reg)
    return reg


def _slot(anchor: Any, protocol: int) -> Dict[str, Any]:
    reg = _registry(anchor)
    slot = reg.get(protocol)
    if slot is None:
        slot = {"candidates": [], "host": None, "host_ident": None, "retired": set()}
        reg[protocol] = slot
    return slot


def register(
    anchor: Any,
    ident: str,
    factory: Callable[[], Any],
    protocol: int = PROTOCOL,
    impl: int = IMPL,
) -> None:
    """Offer this copy as a candidate host. Safe to call more than once.

    `ident` identifies the copy and must be stable across runs; every caller
    passes its module ``__name__``, which is the vendored dotted path and so is
    unique per addon. Re-registering the same ident replaces the old entry
    rather than adding a duplicate, so a module reload during development does
    not stack up dead candidates.
    """
    slot = _slot(anchor, protocol)
    candidates: List[Candidate] = slot["candidates"]
    candidates[:] = [c for c in candidates if c[1] != ident]
    candidates.append((impl, ident, factory))


def elect(anchor: Any, protocol: int = PROTOCOL) -> Optional[Candidate]:
    """The winning candidate, or None if every copy has been retired.

    Highest IMPL wins. Ties -- the normal case, since the same version is
    vendored into every addon -- break on ident, which is a stable alphabetical
    comparison rather than anything to do with import order.
    """
    slot = _slot(anchor, protocol)
    live = [c for c in slot["candidates"] if c[1] not in slot["retired"]]
    if not live:
        return None
    return max(live, key=lambda c: (c[0], c[1]))


def host(anchor: Any, protocol: int = PROTOCOL) -> Optional[Any]:
    """The elected host instance, built on first use and memoised on the anchor.

    Memoising on the anchor rather than in a module global is the whole point:
    every copy has to resolve to the same object.
    """
    slot = _slot(anchor, protocol)
    if slot["host"] is not None:
        return slot["host"]
    winner = elect(anchor, protocol)
    if winner is None:
        return None
    _impl, ident, factory = winner
    instance = factory()
    if instance is None:
        # The winning copy declined to build a host (no Qt, teardown in
        # progress). Retire it so the next post tries the runner-up instead of
        # asking the same broken copy again.
        retire(anchor, ident, protocol)
        return host(anchor, protocol)
    slot["host"] = instance
    slot["host_ident"] = ident
    return instance


def retire(anchor: Any, ident: str, protocol: int = PROTOCOL) -> None:
    """Take a copy out of the running, and drop it if it is the sitting host.

    Called when a host raises: the addon owning it may have been disabled and
    its widgets torn down under us. The next post re-elects from what is left,
    and falls back to aqt's tooltip once nothing is left.
    """
    slot = _slot(anchor, protocol)
    slot["retired"].add(ident)
    if slot["host_ident"] == ident:
        slot["host"] = None
        slot["host_ident"] = None


def current_host_ident(anchor: Any, protocol: int = PROTOCOL) -> Optional[str]:
    """Which copy is hosting, if one has been elected yet. For tests and debug."""
    return _slot(anchor, protocol)["host_ident"]


def reset(anchor: Any) -> None:
    """Forget everything. Tests only."""
    if hasattr(anchor, ANCHOR_ATTR):
        delattr(anchor, ANCHOR_ATTR)
