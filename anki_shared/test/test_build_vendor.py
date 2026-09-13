"""The vendoring steps in `build.py` that run between clearing lib/ and writing its manifest.

That window is what these are about. `clear_previous_vendoring` deletes the tree the previous
manifest describes, and the new manifest is not written until the copies and the checks are
done, so anything that ends the run in between leaves a lib/ that its own manifest lies about -
and that manifest is exactly what `vendor_path.vendor_health` trusts at runtime.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import build  # noqa: E402


def extension(path: Path, body: bytes = b"\x7fELF") -> Path:
    """A file that `build.is_extension` recognises as a compiled half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class TestCopyPerPlatform:
    """`platform_specific_packages` flags a name from any two trees disagreeing, so a
    per-platform package is not guaranteed to exist in every tree - an environment marker is
    enough to leave it out of one. The copy used to raise FileNotFoundError on that."""

    def trees(self, tmp_path, tags=("linux_x86_64", "win_amd64")):
        return {tag: tmp_path / "scratch" / tag for tag in tags}

    def test_a_tree_without_the_package_is_skipped(self, tmp_path):
        trees = self.trees(tmp_path)
        extension(trees["linux_x86_64"] / "uvloop" / "loop.abi3.so")
        lib = tmp_path / "lib"
        build.copy_per_platform(trees, {"uvloop"}, lib)
        assert (lib / "_platform" / "linux_x86_64" / "uvloop" / "loop.abi3.so").is_file()
        assert not (lib / "_platform" / "win_amd64" / "uvloop").exists()

    def test_a_bare_extension_module_is_copied_as_a_file(self, tmp_path):
        """A distribution can install one instead of a package; copytree refuses a file."""
        trees = self.trees(tmp_path)
        for tree in trees.values():
            extension(tree / "_cffi_backend.abi3.so", body=b"\x7fELF" + tree.name.encode())
        lib = tmp_path / "lib"
        build.copy_per_platform(trees, {"_cffi_backend.abi3.so"}, lib)
        for tag, tree in trees.items():
            copied = lib / "_platform" / tag / "_cffi_backend.abi3.so"
            assert copied.read_bytes() == (tree / "_cffi_backend.abi3.so").read_bytes()

    def test_the_copy_it_always_did_still_happens(self, tmp_path):
        trees = self.trees(tmp_path)
        for tree in trees.values():
            extension(tree / "psutil" / "_psutil.abi3.so", body=b"\x7fELF" + tree.name.encode())
            (tree / "psutil" / "__init__.py").write_text("x = 1", "utf-8")
        lib = tmp_path / "lib"
        build.copy_per_platform(trees, {"psutil"}, lib)
        for tag in trees:
            assert (lib / "_platform" / tag / "psutil" / "__init__.py").read_text() == "x = 1"


class TestCheckPerPlatformOutput:
    """The guard above only helps if the check after it agrees that an absent tag is allowed."""

    def per_platform(self, lib, package, bodies):
        for tag, body in bodies.items():
            extension(lib / "_platform" / tag / package / "ext.abi3.so", body=body)

    def test_a_platform_that_has_no_build_is_not_a_failure(self, tmp_path, capsys):
        lib = tmp_path / "lib"
        self.per_platform(lib, "uvloop", {"linux_x86_64": b"a", "macos_arm64": b"b"})
        build.check_per_platform_output(lib, {"uvloop"})
        assert "2 distinct builds across 2 tags" in capsys.readouterr().out

    def test_one_platform_alone_is_not_read_as_one_wheel_for_all_of_them(self, tmp_path):
        lib = tmp_path / "lib"
        self.per_platform(lib, "uvloop", {"linux_x86_64": b"a"})
        build.check_per_platform_output(lib, {"uvloop"})

    def test_identical_builds_across_tags_still_fail(self, tmp_path):
        lib = tmp_path / "lib"
        self.per_platform(lib, "psutil", {tag: b"same" for tag in build.VENDOR_PLATFORMS})
        with pytest.raises(SystemExit) as exit_info:
            build.check_per_platform_output(lib, {"psutil"})
        assert "byte-identical" in str(exit_info.value)

    def test_a_copy_missing_its_compiled_half_still_fails(self, tmp_path):
        lib = tmp_path / "lib"
        pure = lib / "_platform" / "linux_x86_64" / "psutil"
        pure.mkdir(parents=True)
        (pure / "__init__.py").write_text("x = 1", "utf-8")
        with pytest.raises(SystemExit) as exit_info:
            build.check_per_platform_output(lib, {"psutil"})
        assert "no extension module" in str(exit_info.value)

    def test_a_bare_extension_module_counts_as_its_own_build(self, tmp_path):
        lib = tmp_path / "lib"
        for tag, body in (("linux_x86_64", b"a"), ("win_amd64", b"b")):
            extension(lib / "_platform" / tag / "_cffi_backend.abi3.so", body=body)
        build.check_per_platform_output(lib, {"_cffi_backend.abi3.so"})


class TestClearPreviousVendoringDropsTheManifest:
    """Nothing between here and the manifest write may find a manifest describing the old tree."""

    def vendored_lib(self, tmp_path, **overrides):
        lib = tmp_path / "lib"
        (lib / "psutil").mkdir(parents=True)
        (lib / "psutil" / "__init__.py").write_text("old", "utf-8")
        (lib / "mdict_query").mkdir()
        manifest = {"python_version": "3.13", "platforms": ["win_amd64"], "flat": ["psutil"]}
        manifest.update(overrides)
        (lib / build.VENDOR_MANIFEST).write_text(json.dumps(manifest), "utf-8")
        return lib

    def test_the_manifest_does_not_outlive_the_tree_it_described(self, tmp_path):
        lib = self.vendored_lib(tmp_path)
        build.clear_previous_vendoring(lib, {"psutil"}, set(), set())
        assert not (lib / "psutil").exists()
        assert not (lib / build.VENDOR_MANIFEST).exists()

    def test_a_crash_before_the_new_manifest_leaves_no_stale_one(self, tmp_path):
        """`vendor_health` reads a missing manifest as unknown, which is a rebuild offer.

        The alternative is what this closes: a half-rebuilt lib/ still carrying the previous
        manifest, which `build.py dist` would ship and the runtime check would believe.
        """
        lib = self.vendored_lib(tmp_path)
        build.clear_previous_vendoring(lib, {"psutil"}, set(), set())
        with pytest.raises(FileNotFoundError):
            build.copy_tree(tmp_path / "scratch" / "win_amd64" / "psutil", lib / "psutil")
        assert not (lib / build.VENDOR_MANIFEST).exists()

    def test_what_the_manifest_did_not_list_is_still_kept(self, tmp_path):
        """The manifest is read before it is removed, so hand-vendored entries survive."""
        lib = self.vendored_lib(tmp_path)
        build.clear_previous_vendoring(lib, {"psutil"}, set(), set())
        assert (lib / "mdict_query").is_dir()

    def test_removal_is_fine_when_there_was_no_manifest(self, tmp_path):
        lib = tmp_path / "lib"
        (lib / "psutil").mkdir(parents=True)
        build.clear_previous_vendoring(lib, {"psutil"}, set(), set())
        assert not (lib / build.VENDOR_MANIFEST).exists()
