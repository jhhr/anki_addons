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
    """An addon whose packages all arrived, so only manifests are under test.

    The import-based half of health has its own class at the bottom of this file; stubbing it
    here is what keeps every manifest case above from depending on what happens to be
    installed in whatever environment runs the suite.
    """
    monkeypatch.setattr(vendor_path, "_missing_packages", lambda _addon_dir: None)
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

    def test_a_stale_rebuilt_tree_is_demoted_behind_a_healthy_shipped_one(self, addon):
        """It no longer shadows the shipped tree, so the shipped tree is what is live.

        A cp310 rebuilt tree outlives the 3.10 it was built on - Anki's launcher moves Anki's
        Python and `user_files` survives addon updates - and while it went first on sys.path
        it shadowed a shipped `lib/` that was perfectly good. `add_vendor_paths` now puts it
        last, so there is nothing here to rebuild and nothing to report.
        """
        write_manifest(shipped(addon))
        write_manifest(user(addon), python_version=other_python_version())
        assert vendor_path.vendor_health(str(addon)) is None

    def test_neither_tree_fitting_reports_both(self, addon):
        """Then a rebuild really is wanted, and the stale tree is what it will replace."""
        write_manifest(shipped(addon), platforms=["some_other_platform"])
        write_manifest(user(addon), python_version=other_python_version())
        reason = vendor_path.vendor_health(str(addon)) or ""
        assert "the vendored lib" in reason and "the locally rebuilt lib" in reason

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

    def test_a_rebuilt_tree_without_a_manifest_is_demoted_not_trusted(self, addon):
        """The manifest is written last, so a tree without one is an interrupted rebuild.

        It must not go in front of the shipped tree, but with a healthy shipped tree behind
        it there is still nothing to rebuild.
        """
        write_manifest(shipped(addon))
        user(addon).mkdir(parents=True)
        assert "incomplete" in (vendor_path.user_lib_mismatch(str(addon)) or "")
        assert vendor_path.vendor_health(str(addon)) is None

    def test_an_interrupted_rebuild_over_an_unfit_shipped_tree_wants_a_rebuild(self, addon):
        write_manifest(shipped(addon), python_version=other_python_version())
        user(addon).mkdir(parents=True)
        assert "incomplete" in (vendor_path.vendor_health(str(addon)) or "")

    def test_a_rebuilt_tree_from_the_current_requirements_is_healthy(self, addon):
        (addon / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        digest = vendor_path.requirements_digest(str(addon))
        write_manifest(user(addon), rebuilt_locally=True, requirements_sha256=digest)
        assert vendor_path.vendor_health(str(addon)) is None

    def test_a_rebuilt_tree_from_older_requirements_wants_a_rebuild(self, addon):
        """user_files/lib outlives addon updates, so a package added later never reached it."""
        (addon / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        digest = vendor_path.requirements_digest(str(addon))
        (addon / "requirements.txt").write_text("psutil==7.2.2\nsudachipy==0.6.11\n", "utf-8")
        write_manifest(user(addon), rebuilt_locally=True, requirements_sha256=digest)
        assert "requirements.txt" in (vendor_path.vendor_health(str(addon)) or "")

    def test_a_rebuilt_tree_that_recorded_no_requirements_wants_a_rebuild(self, addon):
        (addon / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        write_manifest(user(addon), rebuilt_locally=True)
        assert "requirements.txt" in (vendor_path.vendor_health(str(addon)) or "")

    def test_requirements_line_endings_do_not_count_as_a_change(self, addon):
        (addon / "requirements.txt").write_bytes(b"psutil==7.2.2\n")
        digest = vendor_path.requirements_digest(str(addon))
        (addon / "requirements.txt").write_bytes(b"psutil==7.2.2\r\n")
        assert vendor_path.requirements_digest(str(addon)) == digest

    def test_the_shipped_tree_is_not_held_to_requirements(self, addon):
        """It and requirements.txt arrive in the same zip, so they cannot drift apart."""
        (addon / "requirements.txt").write_text("psutil==7.2.2\n", "utf-8")
        write_manifest(shipped(addon))
        assert vendor_path.vendor_health(str(addon)) is None

    def test_the_import_check_is_only_a_backstop(self, addon, monkeypatch):
        """A matching manifest still fails health when the tree it describes is not there."""
        monkeypatch.setattr(vendor_path, "_missing_packages", lambda _d: "psutil is not in it")
        write_manifest(shipped(addon))
        assert vendor_path.vendor_health(str(addon)) == "psutil is not in it"


class TestMissingPackages:
    """The half of health the manifests cannot answer: did every requirement arrive?

    A tree built before a requirement was added describes its interpreter perfectly, and a
    gitignored `lib/` that never arrived has no manifest to compare at all. Both end in
    ModuleNotFoundError at addon import, which - unlike a package silently falling back to pure
    Python - is not something the addon can carry on through.
    """

    def requirements(self, addon, *lines):
        (addon / "requirements.txt").write_text("\n".join(lines) + "\n", "utf-8")

    def dist_info(self, lib, name):
        lib.mkdir(parents=True, exist_ok=True)
        (lib / f"{name}.dist-info").mkdir()

    def test_a_requirement_that_never_arrived_is_named(self, tmp_path):
        self.requirements(tmp_path, "nonexistent-vendored-pkg==1.0")
        reason = vendor_path._missing_packages(str(tmp_path)) or ""
        assert "nonexistent-vendored-pkg" in reason

    def test_metadata_in_a_live_tree_is_enough(self, tmp_path):
        """What a rebuild would install, installed. Nothing to ask about."""
        self.requirements(tmp_path, "nonexistent-vendored-pkg==1.0")
        self.dist_info(tmp_path / "lib", "nonexistent_vendored_pkg-1.0")
        assert vendor_path._missing_packages(str(tmp_path)) is None

    def test_an_importable_package_is_not_missing_though_it_was_never_vendored(self, tmp_path):
        """Anki ships some of these itself, and a rebuild has nothing to add for those."""
        self.requirements(tmp_path, "json==1.0")
        assert vendor_path._missing_packages(str(tmp_path)) is None

    def test_the_metadata_name_is_matched_however_it_is_spelled(self, tmp_path):
        """`pillow` installs `PIL`: a name mismatch must not read as an absence, or the offer
        would come back at every startup, a successful rebuild never satisfying it."""
        self.requirements(tmp_path, "Json-Repair==0.63.4")
        self.dist_info(tmp_path / "lib", "json_repair-0.63.4")
        assert vendor_path._missing_packages(str(tmp_path)) is None

    def test_a_tree_that_is_on_sys_path_at_all_counts(self, tmp_path):
        """Even demoted, the rebuilt tree is appended rather than dropped, so imports find it."""
        self.requirements(tmp_path, "nonexistent-vendored-pkg==1.0")
        write_manifest(
            tmp_path / "user_files" / "lib",
            python_version=other_python_version(),
            rebuilt_locally=True,
        )
        self.dist_info(tmp_path / "user_files" / "lib", "nonexistent_vendored_pkg-1.0")
        assert vendor_path._missing_packages(str(tmp_path)) is None

    def test_no_requirements_falls_back_to_the_one_module_backstop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vendor_path, "_smoke_test", lambda: "psutil is not in it")
        assert vendor_path._missing_packages(str(tmp_path)) == "psutil is not in it"

    def test_comments_flags_and_markers_are_not_requirements(self, tmp_path):
        """uv writes all three, and two markered lines for one distribution are one name."""
        self.requirements(
            tmp_path,
            "# Compiled from requirements.in by build.py vendor",
            "-r other.txt",
            "",
            "json-repair==0.44.1 ; python_full_version < '3.10'",
            "    # via -r requirements.in",
            "json-repair==0.63.4 ; python_full_version >= '3.10'",
        )
        assert vendor_path.requirement_names(str(tmp_path)) == ["json-repair"]


class TestAddVendorPaths:
    def test_a_fitting_rebuilt_tree_goes_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "path", ["/anki"])
        tag = vendor_path.platform_tag()
        write_manifest(tmp_path / "user_files" / "lib", rebuilt_locally=True)
        (tmp_path / "lib" / "_platform" / str(tag)).mkdir(parents=True)
        vendor_path.add_vendor_paths(str(tmp_path))
        assert sys.path == [
            "/anki",
            str(tmp_path / "user_files" / "lib"),
            str(tmp_path / "lib" / "_platform" / str(tag)),
            str(tmp_path / "lib"),
        ]

    def test_a_rebuilt_tree_that_does_not_fit_goes_last(self, tmp_path, monkeypatch):
        """Behind the shipped tree it shadows nothing, which is the point of demoting it.

        In front, its cp310 extensions were what `import psutil` found on a 3.13 runtime, and
        the shipped tree that was built for 3.13 never got a look in.
        """
        monkeypatch.setattr(sys, "path", ["/anki"])
        tag = vendor_path.platform_tag()
        write_manifest(
            tmp_path / "user_files" / "lib",
            python_version=other_python_version(),
            rebuilt_locally=True,
        )
        (tmp_path / "lib" / "_platform" / str(tag)).mkdir(parents=True)
        vendor_path.add_vendor_paths(str(tmp_path))
        assert sys.path == [
            "/anki",
            str(tmp_path / "lib" / "_platform" / str(tag)),
            str(tmp_path / "lib"),
            str(tmp_path / "user_files" / "lib"),
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
