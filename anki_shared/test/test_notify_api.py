"""Routing: which of the elected host and the tooltip fallback gets a call.

The host belongs to a different addon than the caller, so the interesting
cases are the ones where it is absent or breaks mid-call. None of them may
raise: this runs inside sync_did_finish.
"""

import pytest

from anki_shared import notify
from anki_shared.notify import fallback, registry


class Anchor:
    """Stands in for aqt.mw, fresh per test so registry state cannot leak."""


class FakeHost:
    """The interface host.py has to satisfy."""

    def __init__(self, label="host"):
        self.label = label
        self.posted = []
        self.updated = []
        self.dismissed = []

    def post(self, spec):
        self.posted.append(spec)
        return f"{self.label}-{len(self.posted)}"

    def update(self, handle, fields):
        self.updated.append((handle, fields))
        return True

    def dismiss(self, handle):
        self.dismissed.append(handle)
        return True


class ExplodingHost:
    def __init__(self):
        self.calls = 0

    def post(self, spec):
        self.calls += 1
        raise RuntimeError("widget deleted under us")

    def update(self, handle, fields):
        raise RuntimeError("widget deleted under us")

    def dismiss(self, handle):
        raise RuntimeError("widget deleted under us")


@pytest.fixture
def anchor(monkeypatch):
    obj = Anchor()
    monkeypatch.setattr(notify, "_anchor", lambda: obj)
    return obj


@pytest.fixture
def shown(monkeypatch):
    """Capture what the fallback would have handed to aqt's tooltip."""
    captured = []
    monkeypatch.setattr(fallback, "_show", captured.append)
    fallback._recent.clear()
    return captured


class TestWithNoHost:
    def test_post_falls_back_to_the_tooltip(self, anchor, shown):
        handle = notify.post(source="Copy Anywhere", title="done")
        assert fallback.owns(handle)
        assert len(shown) == 1
        assert shown[0]["title"] == "done"

    def test_a_missing_window_still_does_not_raise(self, monkeypatch, shown):
        # Before mw exists there is nowhere to register or elect.
        monkeypatch.setattr(notify, "_anchor", lambda: None)
        assert fallback.owns(notify.post(source="A", title="done"))


class TestWithAHost:
    def test_post_goes_to_the_elected_host(self, anchor, shown):
        host = FakeHost()
        registry.register(anchor, "addon_a", lambda: host)
        handle = notify.post(source="Copy Anywhere", title="done")
        assert handle == "host-1"
        assert shown == []
        assert host.posted[0]["source"] == "Copy Anywhere"

    def test_update_and_dismiss_reach_the_host(self, anchor, shown):
        host = FakeHost()
        registry.register(anchor, "addon_a", lambda: host)
        handle = notify.post(source="A", title="first")
        assert notify.update(handle, title="second") is True
        assert notify.dismiss(handle) is True
        assert host.updated == [(handle, {"title": "second"})]
        assert host.dismissed == [handle]

    def test_every_copy_posts_into_the_one_host(self, anchor, shown):
        # The whole point: two addons, one stack.
        host = FakeHost()
        registry.register(anchor, "addon_a", lambda: host)
        registry.register(anchor, "addon_b", lambda: FakeHost("other"))
        notify.post(source="Copy Anywhere", title="a")
        notify.post(source="Related Card Disperse", title="b")
        winner = registry.host(anchor)
        assert len(winner.posted) == 2


class TestHostFailure:
    def test_a_raising_host_is_retired_and_the_runner_up_takes_over(self, anchor, shown):
        survivor = FakeHost("survivor")
        registry.register(anchor, "addon_aaa", lambda: survivor)
        registry.register(anchor, "addon_zzz", ExplodingHost)  # wins on ident
        handle = notify.post(source="A", title="done")
        assert handle == "survivor-1"
        assert shown == []
        assert registry.current_host_ident(anchor) == "addon_aaa"

    def test_the_retirement_sticks_for_later_posts(self, anchor, shown):
        survivor = FakeHost("survivor")
        registry.register(anchor, "addon_aaa", lambda: survivor)
        exploding = ExplodingHost()
        registry.register(anchor, "addon_zzz", lambda: exploding)
        notify.post(source="A", title="one")
        notify.post(source="A", title="two")
        # The broken copy is asked once, not once per post.
        assert exploding.calls == 1
        assert len(survivor.posted) == 2

    def test_every_host_failing_falls_back(self, anchor, shown):
        registry.register(anchor, "addon_a", ExplodingHost)
        registry.register(anchor, "addon_b", ExplodingHost)
        handle = notify.post(source="A", title="done")
        assert fallback.owns(handle)
        assert len(shown) == 1

    def test_update_on_a_dead_host_reports_failure_without_raising(self, anchor, shown):
        registry.register(anchor, "addon_a", ExplodingHost)
        assert notify.update("host-1", title="x") is False
        assert notify.dismiss("host-1") is False


class TestFallbackHandles:
    def test_a_fallback_handle_keeps_using_the_fallback(self, anchor, shown):
        handle = notify.post(source="A", title="first")
        # A host arriving later must not be handed a handle it never minted.
        registry.register(anchor, "addon_a", FakeHost)
        assert notify.update(handle, title="second") is True
        assert shown[-1]["title"] == "second"

    def test_update_merges_rather_than_replacing(self, anchor, shown):
        handle = notify.post(source="A", title="first", body="detail")
        notify.update(handle, title="second")
        assert shown[-1]["body"] == "detail"

    def test_dismissing_an_unknown_handle_is_false(self, anchor, shown):
        assert notify.dismiss("fb:999") is False
        assert notify.update("fb:999", title="x") is False
