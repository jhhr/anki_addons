"""Fetching the word array generator's data files, without the network.

Downloads are driven through file:// URLs, which urllib serves like any other, so what is under
test is the part that matters when a real download goes wrong: a bad hash or an interrupted
transfer must leave nothing behind that a later run would take for the real file.
"""

import gzip
import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from addon_modules import load_ops_module

resources = load_ops_module("resources", subdir="word_array")
jmdict_index = load_ops_module("jmdict_index", subdir="word_array")

NOOP = lambda _message: None  # noqa: E731


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_file(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path


class DownloadTests(TempDirTest):
    def test_a_download_matching_its_hash_is_kept(self):
        src = self.write_file("src.bin", b"dictionary bytes")
        dest = self.dir / "out" / "dest.bin"
        sha = hashlib.sha256(b"dictionary bytes").hexdigest()
        resources._download(src.as_uri(), dest, resources.SUDACHI_DOWNLOAD, NOOP, sha)
        self.assertEqual(dest.read_bytes(), b"dictionary bytes")

    def test_a_hash_mismatch_leaves_nothing_behind(self):
        src = self.write_file("src.bin", b"tampered")
        dest = self.dir / "out" / "dest.bin"
        with self.assertRaises(OSError):
            resources._download(src.as_uri(), dest, resources.SUDACHI_DOWNLOAD, NOOP, "0" * 64)
        self.assertEqual(os.listdir(dest.parent), [])

    def test_the_dictionary_is_unpacked_from_the_wheel_and_the_wheel_removed(self):
        wheel = self.dir / "sudachidict_core.whl"
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr(resources.SUDACHI_DICT_MEMBER, b"system.dic bytes")
            z.writestr("sudachidict_core/__init__.py", b"")
        sudachi_dir = self.dir / "sudachi"
        with mock.patch.multiple(
            resources,
            SUDACHI_DICT_URL=wheel.as_uri(),
            SUDACHI_DICT_SHA256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            SUDACHI_DIR=sudachi_dir,
            SUDACHI_DIC=sudachi_dir / "system_core.dic",
        ):
            resources._fetch_sudachi_dictionary(NOOP)
        self.assertEqual(os.listdir(sudachi_dir), ["system_core.dic"])
        self.assertEqual((sudachi_dir / "system_core.dic").read_bytes(), b"system.dic bytes")


class SudachiDictionaryChoiceTests(TempDirTest):
    def choose(self, env: dict, downloaded: bool, installed: set) -> object:
        dic = self.dir / "system_core.dic"
        if downloaded:
            dic.write_bytes(b"")

        def find_spec(name):
            return object() if name in installed else None

        # patch.dict restores os.environ afterwards, the pop included
        with mock.patch.dict(os.environ, env), mock.patch.object(
            resources, "SUDACHI_DIC", dic
        ), mock.patch.object(resources.importlib.util, "find_spec", find_spec):
            if "SUDACHI_DICT" not in env:
                os.environ.pop("SUDACHI_DICT", None)
            return resources.sudachi_dictionary()

    def test_the_environment_variable_wins(self):
        chosen = self.choose({"SUDACHI_DICT": "small"}, True, {"sudachidict_full"})
        self.assertEqual(chosen, "small")

    def test_the_downloaded_dictionary_beats_an_installed_package(self):
        chosen = self.choose({}, True, {"sudachidict_full"})
        self.assertEqual(chosen, str(self.dir / "system_core.dic"))

    def test_an_installed_package_is_used_when_nothing_was_downloaded(self):
        self.assertEqual(self.choose({}, False, {"sudachidict_full"}), "full")

    def test_none_when_there_is_no_dictionary_at_all(self):
        self.assertIsNone(self.choose({}, False, set()))


JMDICT_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE JMdict [
<!ELEMENT JMdict (entry*)>
<!ENTITY exp "expressions (phrases, clauses, etc.)">
<!ENTITY v5r "Godan verb with 'ru' ending">
]>
<JMdict>
<entry>
<k_ele><keb>様に成る</keb></k_ele>
<r_ele><reb>ようになる</reb></r_ele>
<sense><pos>&exp;</pos><pos>&v5r;</pos></sense>
</entry>
</JMdict>
"""


class JmdictBuildTests(TempDirTest):
    def test_entries_are_indexed_by_every_form_with_pos_codes_kept(self):
        gz = self.dir / "JMdict_e.gz"
        with gzip.open(gz, "wt", encoding="utf-8") as f:
            f.write(JMDICT_SAMPLE)
        index = jmdict_index.build(gz)
        expected = [(("ようになる",), frozenset({"exp", "v5r"}))]
        self.assertEqual(index, {"様に成る": expected, "ようになる": expected})


if __name__ == "__main__":
    unittest.main()
