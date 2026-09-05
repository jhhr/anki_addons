"""The vendored-lib health check, which decides whether to offer a local rebuild.

Every case here is a manifest that does or does not describe the running interpreter. There
are deliberately no import-based cases: the breakage this check exists for - cp39 binaries
under a 3.13 runtime - imports perfectly well and silently degrades, which is why the check
compares recorded build targets instead.
"""

import json
import sys

import pytest

from anki_shared.utils import vendor_path


def other_python_version() -> str:
    """A version that is never the one running, whichever interpreter runs the suite.

    It used to be the literal "3.12", which stopped testing anything the day the interpreter
    running the tests became 3.12: the manifest matched, the tree came out healthy, and the
    two tests asserting a rebuild was wanted failed.
    """
    major, minor = vendor_path.runtime_python_version().split(".")
    return f"{major}.{int(minor) + 1}"


def write_manifest(lib, **overrides):
    manifest = {
        "python_version": vendor_path.runtime_python_version(),
        "platforms": [vendor_path.platform_tag()],
        "flat": ["psutil"],
        "per_platform": [],
    }
    manifest.update(overrides)
    lib.mkdir(parents=True, exist_ok=True)
    (lib / vendor_path.VENDOR_MANIFEST).write_text(json.dumps(manifest), "utf-8")
    return lib


@pytest.fixture
def addon(tmp_path, monkeypatch):
    """An addon directory whose smoke import always passes, so only manifests are under test."""
    monkeypatch.setattr(vendor_path, "_smoke_test", lambda: None)
    return tmp_path


def shipped(addon):
    return addon / "lib"


def user(addon):
    return addon / "user_files" / "lib"


class TestVendorHealth:
    def test_matching_shipped_manifest_is_healthy(self, addon):
        write_manifest(shipped(addon))
        assert vendor_path.vendor_health(str(addon)) is None

    def test_matching_user_manifest_is_healthy(self, addon):
        write_manifest(user(addon))
        assert vendor_path.vendor_health(str(addon)) is None

    def test_no_manifest_anywhere_wants_a_rebuild(self, addon):
        shipped(addon).mkdir(parents=True)
        assert "no manifest" in (vendor_path.vendor_health(str(addon)) or "")

    def test_wrong_python_version_wants_a_rebuild(self, addon):
        built_for = other_python_version()
        write_manifest(shipped(addon), python_version=built_for)
        reason = vendor_path.vendor_health(str(addon)) or ""
        assert built_for in reason and "Python" in reason

    def test_missing_python_version_wants_a_rebuild(self, addon):
        write_manifest(shipped(addon))
        manifest_file = shipped(addon) / vendor_path.VENDOR_MANIFEST
        manifest_file.write_text(json.dumps({"platforms": [vendor_path.platform_tag()]}), "utf-8")
        assert vendor_path.vendor_health(str(addon)) is not None

    def test_platform_not_in_manifest_wants_a_rebuild(self, addon):
        write_manifest(shipped(addon), platforms=["some_other_platform"])
        assert vendor_path.platform_tag() in (vendor_path.vendor_health(str(addon)) or "")

    def test_unknown_platform_wants_a_rebuild(self, addon, monkeypatch):
        monkeypatch.setattr(vendor_path, "platform_tag", lambda: None)
        write_manifest(shipped(addon))
        assert vendor_path.vendor_health(str(addon)) is not None

    def test_malformed_manifest_wants_a_rebuild(self, addon):
        shipped(addon).mkdir(parents=True)
        (shipped(addon) / vendor_path.VENDOR_MANIFEST).write_text("{not json", "utf-8")
        assert "no manifest" in (vendor_path.vendor_health(str(addon)) or "")

    def test_manifest_that_is_not_an_object_wants_a_rebuild(self, addon):
        shipped(addon).mkdir(parents=True)
        (shipped(addon) / vendor_path.VENDOR_MANIFEST).write_text("[]", "utf-8")
        assert vendor_path.vendor_health(str(addon)) is not None

    def test_a_stale_rebuilt_tree_is_not_rescued_by_a_healthy_shipped_one(self, addon):
        """It is first on sys.path, so it shadows the shipped tree rather than backing it up."""
        write_manifest(shipped(addon))
        write_manifest(user(addon), python_version=other_python_version())
        assert "rebuilt" in (vendor_path.vendor_health(str(addon)) or "")

    def test_a_rebuilt_tree_fits_a_platform_no_build_is_shipped_for(self, addon, monkeypatch):
        """It was built here, so there is no shipped platform tag to hold it to.

        Holding it to one was wrong in exactly the case a rebuild exists for: with no tag for
        this machine, the rebuild records None and the check then rejects the tree that fits
        best. A successful rebuild also clears the record that stops the offer coming back, so
        the same rebuild was offered again at every startup, forever.
        """
        monkeypatch.setattr(vendor_path, "platform_tag", lambda: None)
        write_manifest(user(addon), platforms=[None], rebuilt_locally=True)
        assert vendor_path.vendor_health(str(addon)) is None

    def test_a_rebuilt_tree_from_another_machine_wants_a_rebuild(self, addon):
        """user_files is what Anki carries across an update, and what people copy about."""
        write_manifest(user(addon), platforms=["some_other_platform"], rebuilt_locally=True)
        assert "some_other_platform" in (vendor_path.vendor_health(str(addon)) or "")

    def test_a_rebuilt_tree_is_still_held_to_the_python_it_was_built_for(self, addon):
        """The exemption is about platform tags only; the launcher can still move Python."""
        write_manifest(
            user(addon), python_version=other_python_version(), rebuilt_locally=True
        )
        assert "Python" in (vendor_path.vendor_health(str(addon)) or "")

    def test_rebuilt_tree_without_a_manifest_wants_a_rebuild(self, addon):
        """The manifest is written last, so a tree without one is an interrupted rebuild."""
        write_manifest(shipped(addon))
        user(addon).mkdir(parents=True)
        assert "incomplete" in (vendor_path.vendor_health(str(addon)) or "")

    def test_the_smoke_import_is_only_a_backstop(self, addon, monkeypatch):
        """A matching manifest still fails health when the tree it describes is not there."""
        monkeypatch.setattr(vendor_path, "_smoke_test", lambda: "psutil is not in it")
        write_manifest(shipped(addon))
        assert vendor_path.vendor_health(str(addon)) == "psutil is not in it"


class TestAddVendorPaths:
    def test_layers_are_appended_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "path", ["/anki"])
        tag = vendor_path.platform_tag()
        (tmp_path / "user_files" / "lib").mkdir(parents=True)
        (tmp_path / "lib" / "_platform" / str(tag)).mkdir(parents=True)
        vendor_path.add_vendor_paths(str(tmp_path))
        assert sys.path == [
            "/anki",
            str(tmp_path / "user_files" / "lib"),
            str(tmp_path / "lib" / "_platform" / str(tag)),
            str(tmp_path / "lib"),
        ]

    def test_absent_layers_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "path", [])
        (tmp_path / "lib").mkdir()
        vendor_path.add_vendor_paths(str(tmp_path))
        assert sys.path == [str(tmp_path / "lib")]

    def test_calling_twice_does_not_duplicate_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "path", [])
        (tmp_path / "lib").mkdir()
        vendor_path.add_vendor_paths(str(tmp_path))
        vendor_path.add_vendor_paths(str(tmp_path))
        assert sys.path == [str(tmp_path / "lib")]
