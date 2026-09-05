"""Ordering, grouping, dedup and queueing for the shared toast stack.

These rules are the ones a host would otherwise get subtly wrong in a way that
only shows up as two addons' sync results fighting over a slot, so they live in
a Qt-free module and are exercised directly.
"""

from anki_shared.notify.policy import Stack
from anki_shared.notify.spec import HOST_DEFAULT, STICKY, make_spec, merge_spec


def spec(source="Copy Anywhere", title="done", **kwargs):
    return make_spec(source=source, title=title, **kwargs)


class TestMakeSpec:
    def test_unknown_level_degrades_to_info(self):
        # Never raise: this runs inside sync_did_finish.
        assert spec(level="catastrophe")["level"] == "info"

    def test_unknown_keys_are_kept_aside(self):
        result = spec(icon="star", sound=True)
        assert result["extra"] == {"icon": "star", "sound": True}

    def test_malformed_actions_are_dropped(self):
        result = spec(actions=[("Open", lambda: None), "nope", ("no callback", 3)])
        assert [label for label, _ in result["actions"]] == ["Open"]

    def test_a_string_of_actions_is_not_iterated_as_pairs(self):
        assert spec(actions="Open")["actions"] == ()

    def test_negative_timeout_normalises_to_sticky(self):
        assert spec(timeout_ms=-500)["timeout_ms"] == STICKY

    def test_a_bool_timeout_is_not_taken_as_a_duration(self):
        # True is an int; silently posting a 1ms toast would be baffling.
        assert spec(timeout_ms=True)["timeout_ms"] == HOST_DEFAULT

    def test_merge_keeps_unstated_fields_and_unions_extra(self):
        old = spec(title="first", body="detail", icon="a")
        new = make_spec(source="Copy Anywhere", title="second", sound=True)
        merged = merge_spec(old, new)
        assert merged["title"] == "second"
        assert merged["extra"] == {"icon": "a", "sound": True}


class TestKeyedReposts:
    def test_a_repost_under_the_same_key_replaces_in_place(self):
        # copy_anywhere posts its local-changes result, then updates the same
        # entry when the remote pass finishes, rather than adding a second one.
        stack = Stack()
        first = stack.post(spec(title="local changes", key="sync"), now_ms=0)
        second = stack.post(spec(title="remote changes", key="sync"), now_ms=500)
        assert first == second
        assert len(stack.ordered()) == 1
        assert stack.entry(first)["spec"]["title"] == "remote changes"

    def test_a_repost_restarts_the_countdown(self):
        stack = Stack()
        handle = stack.post(spec(key="sync"), now_ms=0)
        stack.post(spec(key="sync"), now_ms=4000)
        assert stack.entry(handle)["posted_ms"] == 4000

    def test_keys_are_scoped_to_their_source(self):
        stack = Stack()
        stack.post(spec(source="Copy Anywhere", key="sync"), now_ms=0)
        stack.post(spec(source="Related Card Disperse", key="sync"), now_ms=0)
        assert len(stack.ordered()) == 2

    def test_an_empty_key_never_dedups(self):
        stack = Stack()
        stack.post(spec(), now_ms=0)
        stack.post(spec(), now_ms=0)
        assert len(stack.ordered()) == 2


class TestGrouping:
    def test_posts_within_the_window_share_a_group(self):
        stack = Stack(group_window_ms=5000)
        a = stack.post(spec(source="A", group="sync"), now_ms=0)
        b = stack.post(spec(source="B", group="sync"), now_ms=3000)
        assert stack.entry(a)["group_id"] == stack.entry(b)["group_id"]

    def test_a_late_arrival_extends_the_window_from_the_last_post(self):
        # addon_config_sync posts from the media sync hook, well after the rest.
        stack = Stack(group_window_ms=5000)
        a = stack.post(spec(source="A", group="sync"), now_ms=0)
        b = stack.post(spec(source="B", group="sync"), now_ms=4000)
        c = stack.post(spec(source="C", group="sync"), now_ms=8000)
        assert stack.entry(a)["group_id"] == stack.entry(c)["group_id"]
        assert stack.entry(b)["group_id"] == stack.entry(c)["group_id"]

    def test_a_later_sync_starts_a_new_group(self):
        stack = Stack(group_window_ms=5000)
        a = stack.post(spec(group="sync"), now_ms=0)
        b = stack.post(spec(group="sync"), now_ms=60000)
        assert stack.entry(a)["group_id"] != stack.entry(b)["group_id"]

    def test_ungrouped_posts_never_share_a_group(self):
        stack = Stack()
        a = stack.post(spec(), now_ms=0)
        b = stack.post(spec(), now_ms=0)
        assert stack.entry(a)["group_id"] != stack.entry(b)["group_id"]

    def test_group_members_lists_the_siblings_on_screen(self):
        stack = Stack()
        a = stack.post(spec(source="A", group="sync"), now_ms=0)
        stack.post(spec(source="B", group="sync"), now_ms=100)
        stack.post(spec(source="C"), now_ms=200)
        assert len(stack.group_members(stack.entry(a))) == 2

    def test_pruning_a_group_does_not_corrupt_a_surviving_one(self):
        # Regression: deriving the next group id from len(_groups) reused the
        # id of a live bucket once an earlier one had been pruned.
        stack = Stack()
        a = stack.post(spec(group="first"), now_ms=0)
        b = stack.post(spec(group="second"), now_ms=60000)
        stack.dismiss(a)
        stack.post(spec(group="third"), now_ms=120000)
        assert stack.group_name(stack.entry(b)) == "second"


class TestVisibility:
    def test_entries_beyond_the_limit_are_queued(self):
        stack = Stack(max_visible=2)
        for i in range(4):
            stack.post(spec(title=str(i)), now_ms=i)
        assert [e["spec"]["title"] for e in stack.visible()] == ["0", "1"]
        assert [e["spec"]["title"] for e in stack.queued()] == ["2", "3"]

    def test_dismissing_promotes_the_next_in_the_queue(self):
        stack = Stack(max_visible=2)
        handles = [stack.post(spec(title=str(i)), now_ms=i) for i in range(3)]
        stack.dismiss(handles[0])
        assert [e["spec"]["title"] for e in stack.visible()] == ["1", "2"]

    def test_a_pinned_entry_keeps_its_slot(self):
        # The user asked for it to stay; a burst must not push it into the queue.
        stack = Stack(max_visible=2)
        handles = [stack.post(spec(title=str(i)), now_ms=i) for i in range(2)]
        stack.set_pinned(handles[0], True)
        stack.post(spec(title="new"), now_ms=10)
        assert handles[0] in [e["handle"] for e in stack.visible()]

    def test_ordering_follows_when_each_group_started(self):
        stack = Stack(max_visible=10, group_window_ms=5000)
        stack.post(spec(title="a1", group="a"), now_ms=0)
        stack.post(spec(title="b1", group="b"), now_ms=100)
        stack.post(spec(title="a2", group="a"), now_ms=200)
        # a2 joins the older group, so it sorts above b1 rather than last.
        assert [e["spec"]["title"] for e in stack.visible()] == ["a1", "a2", "b1"]


class TestTimeouts:
    def test_host_default_is_applied(self):
        stack = Stack(default_timeout_ms=7000)
        handle = stack.post(spec(), now_ms=0)
        assert stack.timeout_ms(stack.entry(handle)) == 7000

    def test_an_explicit_timeout_is_honoured(self):
        stack = Stack()
        handle = stack.post(spec(timeout_ms=12000), now_ms=0)
        assert stack.timeout_ms(stack.entry(handle)) == 12000

    def test_sticky_never_expires(self):
        stack = Stack()
        handle = stack.post(spec(timeout_ms=STICKY), now_ms=0)
        assert stack.timeout_ms(stack.entry(handle)) is None

    def test_pinning_suspends_expiry(self):
        stack = Stack()
        handle = stack.post(spec(), now_ms=0)
        stack.set_pinned(handle, True)
        assert stack.timeout_ms(stack.entry(handle)) is None


class TestMissingHandles:
    def test_operations_on_an_unknown_handle_report_failure(self):
        stack = Stack()
        assert stack.entry("nope") is None
        assert stack.dismiss("nope") is False
        assert stack.update("nope", {}) is False
        assert stack.set_pinned("nope", True) is False
