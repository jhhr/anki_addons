"""dev/data_paths.py: where the private test data checkout is found."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Optional
from unittest import mock

from addon_modules import ADDON_ROOT


def load_data_paths() -> ModuleType:
    """A copy of dev/data_paths.py of this test's own, loaded by path as the research scripts
    load it."""
    spec = importlib.util.spec_from_file_location(
        "jnaio_data_paths_under_test", ADDON_ROOT / "dev" / "data_paths.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DataRootTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name) / "repo"
        self.repo.mkdir()
        self.paths = load_data_paths()
        for name, value in (("REPO_ROOT", self.repo), ("DEFAULT_ROOT", self.repo / "test_data")):
            patch = mock.patch.object(self.paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def root(self, configured: Optional[str] = None) -> Optional[Path]:
        """data_root() with the variable set to `configured`, or unset."""
        variable = self.paths.DATA_ROOT_ENV
        with mock.patch.dict(os.environ, {} if configured is None else {variable: configured}):
            if configured is None:
                os.environ.pop(variable, None)
            return self.paths.data_root()

    def test_the_repo_s_test_data_is_the_default(self):
        (self.repo / "test_data").mkdir()

        self.assertEqual(self.root(), self.repo / "test_data")

    def test_no_checkout_is_none(self):
        self.assertIsNone(self.root())

    def test_a_relative_variable_is_taken_from_the_repo_root(self):
        # Not from the cwd: the dev scripts start in the addon, pytest in the repo root
        (self.repo / "elsewhere").mkdir()

        self.assertEqual(self.root("elsewhere"), self.repo / "elsewhere")

    def test_an_absolute_variable_is_taken_as_it_is(self):
        elsewhere = self.repo.parent / "elsewhere"
        elsewhere.mkdir()

        self.assertEqual(self.root(str(elsewhere)), elsewhere)

    def test_a_variable_naming_no_directory_is_an_error(self):
        # Read as "no checkout", a mistyped one skipped every private fixture and moved the
        # eval data to output/ without a word, even with a checkout at the default
        (self.repo / "test_data").mkdir()

        with self.assertRaisesRegex(FileNotFoundError, self.paths.DATA_ROOT_ENV):
            self.root("test_dta")


if __name__ == "__main__":
    unittest.main()
