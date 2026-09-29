"""Where the research scripts keep their eval data: the private test data checkout's evals/,
else output/ (word_array/research/_bootstrap.eval_file)."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import _bootstrap  # noqa: E402

NAME = "judge_eval_results.jsonl"


class EvalFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.checkout = self.root / "test_data"
        self.evals = self.checkout / "japanese_note_ai_ops" / "evals"
        patch = mock.patch.object(_bootstrap, "OUTPUT", self.output)
        patch.start()
        self.addCleanup(patch.stop)

    def resolve(self, checkout: Path) -> tuple[Path, str]:
        warnings = io.StringIO()
        with mock.patch.dict(os.environ, {"ANKI_ADDONS_TEST_DATA": str(checkout)}):
            with contextlib.redirect_stderr(warnings):
                path = _bootstrap.eval_file(NAME)
        return path, warnings.getvalue()

    def test_without_a_checkout_it_is_output_as_before(self):
        path, warnings = self.resolve(self.root / "no_such_checkout")

        self.assertEqual(path, self.output / NAME)
        self.assertEqual(warnings, "")

    def test_with_a_checkout_a_new_file_is_made_in_evals(self):
        self.evals.mkdir(parents=True)

        path, warnings = self.resolve(self.checkout)

        self.assertEqual(path, self.evals / NAME)
        self.assertEqual(warnings, "")

    def test_a_file_only_output_has_is_used_there_and_named_to_be_moved(self):
        self.evals.mkdir(parents=True)
        (self.output / NAME).write_text("{}\n", encoding="utf-8")

        path, warnings = self.resolve(self.checkout)

        self.assertEqual(path, self.output / NAME)
        self.assertIn("move it", warnings)

    def test_one_both_have_is_evals_and_a_newer_output_copy_is_named(self):
        self.evals.mkdir(parents=True)
        kept, local = self.evals / NAME, self.output / NAME
        kept.write_text("{}\n", encoding="utf-8")
        local.write_text("{}\n", encoding="utf-8")
        os.utime(kept, (1_000_000, 1_000_000))
        os.utime(local, (2_000_000, 2_000_000))

        path, warnings = self.resolve(self.checkout)

        self.assertEqual(path, kept)
        self.assertIn("newer copy", warnings)

        os.utime(local, (500_000, 500_000))
        self.assertEqual(self.resolve(self.checkout), (kept, ""))


if __name__ == "__main__":
    unittest.main()
