"""The election has to survive being run from several copies of its own file.

build.py vendors anki_shared/notify into each addon, so at runtime there are as
many registry modules as there are addons. These tests reproduce that by
loading registry.py repeatedly under different module names -- which is exactly
what Python's import machinery does to it in a real profile -- and assert that
the copies still converge on one host.
"""

import importlib.util
import os

REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notify", "registry.py"
)


def load_copy(name):
    """A separate module object from the same file, as vendoring produces."""
    spec = importlib.util.spec_from_file_location(name, REGISTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Anchor:
    """Stands in for aqt.mw: the one object every addon copy shares."""


def factory_for(label):
    """A host factory whose product identifies the copy that built it."""
    return lambda: {"built_by": label}


class TestElection:
    def test_copies_converge_on_one_host(self):
        a, b = load_copy("copy_a"), load_copy("copy_b")
        anchor = Anchor()

        a.register(anchor, "addon_a.shared.notify.registry", factory_for("a"))
        b.register(anchor, "addon_b.shared.notify.registry", factory_for("b"))

        # The point of the whole design: asking either copy gives one object.
        assert a.host(anchor) is b.host(anchor)

    def test_outcome_does_not_depend_on_registration_order(self):
        winners = []
        for order in ((("a", 1), ("b", 1)), (("b", 1), ("a", 1))):
            anchor = Anchor()
            module = load_copy("copy_order")
            for label, impl in order:
                module.register(
                    anchor, f"addon_{label}.shared.notify.registry",
                    factory_for(label), impl=impl,
                )
            winners.append(module.host(anchor)["built_by"])
        # Anki sorts addon dirs alphabetically and ANKIREVADDONS reverses them,
        # so both orders happen in the wild and must agree.
        assert winners[0] == winners[1]

    def test_highest_impl_wins_regardless_of_order(self):
        for order in ((1, 5), (5, 1)):
            anchor = Anchor()
            module = load_copy("copy_impl")
            module.register(anchor, "addon_old", factory_for("old"), impl=order[0])
            module.register(anchor, "addon_new", factory_for("new"), impl=order[1])
            expected = "new" if order == (1, 5) else "old"
            assert module.host(anchor)["built_by"] == expected

    def test_equal_impl_breaks_on_ident(self):
        # The normal case: one version vendored into every addon.
        anchor = Anchor()
        module = load_copy("copy_tie")
        module.register(anchor, "addon_aaa", factory_for("aaa"))
        module.register(anchor, "addon_zzz", factory_for("zzz"))
        assert module.host(anchor)["built_by"] == "zzz"

    def test_host_is_built_once(self):
        anchor = Anchor()
        module = load_copy("copy_once")
        calls = []

        def factory():
            calls.append(1)
            return {"built_by": "a"}

        module.register(anchor, "addon_a", factory)
        module.host(anchor)
        module.host(anchor)
        assert len(calls) == 1

    def test_no_candidates_yields_no_host(self):
        module = load_copy("copy_empty")
        assert module.host(Anchor()) is None


class TestRegistration:
    def test_reregistering_an_ident_replaces_it(self):
        anchor = Anchor()
        module = load_copy("copy_dup")
        module.register(anchor, "addon_a", factory_for("first"))
        module.register(anchor, "addon_a", factory_for("second"))
        assert len(module.elect(anchor)) == 3
        assert module.host(anchor)["built_by"] == "second"

    def test_protocols_elect_separately(self):
        anchor = Anchor()
        module = load_copy("copy_proto")
        module.register(anchor, "addon_old", factory_for("old"), protocol=1, impl=99)
        module.register(anchor, "addon_new", factory_for("new"), protocol=2, impl=1)
        assert module.host(anchor, protocol=1)["built_by"] == "old"
        assert module.host(anchor, protocol=2)["built_by"] == "new"


class TestRetirement:
    def test_retiring_the_host_promotes_the_runner_up(self):
        anchor = Anchor()
        module = load_copy("copy_retire")
        module.register(anchor, "addon_aaa", factory_for("aaa"))
        module.register(anchor, "addon_zzz", factory_for("zzz"))
        assert module.host(anchor)["built_by"] == "zzz"

        # As happens when the hosting addon is disabled and its widgets go.
        module.retire(anchor, "addon_zzz")
        assert module.host(anchor)["built_by"] == "aaa"

    def test_retiring_every_copy_yields_no_host(self):
        anchor = Anchor()
        module = load_copy("copy_retire_all")
        module.register(anchor, "addon_a", factory_for("a"))
        module.retire(anchor, "addon_a")
        # The caller's cue to fall back to aqt.utils.tooltip.
        assert module.host(anchor) is None

    def test_a_factory_returning_none_is_skipped(self):
        anchor = Anchor()
        module = load_copy("copy_declines")
        module.register(anchor, "addon_aaa", factory_for("aaa"))
        module.register(anchor, "addon_zzz", lambda: None)
        assert module.host(anchor)["built_by"] == "aaa"
        assert module.current_host_ident(anchor) == "addon_aaa"


class TestReset:
    def test_reset_forgets_everything(self):
        anchor = Anchor()
        module = load_copy("copy_reset")
        module.register(anchor, "addon_a", factory_for("a"))
        module.host(anchor)
        module.reset(anchor)
        assert module.host(anchor) is None
