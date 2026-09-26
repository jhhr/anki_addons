"""The one way a chain's step is failed on an exception: `step_failure.failed_step_outcome`.

It runs while the chain's progress dialog is open, so the error goes to that dialog's pane
when `report_run_error` takes it, and to aqt's error box when it does not. Whatever happens
while showing it, the chain gets its outcome. The call sites are checked in
test_op_chain_step (an op's run), test_op_chain (a step's start) and test_op_registry (the
word array download).
"""

from __future__ import annotations

import unittest
from unittest import mock

from addon_modules import load_ops_module

chain_types = load_ops_module("chain_types")
step_failure = load_ops_module("step_failure")
api = load_ops_module("api_client")

PARENT = object()
TITLE = "Step 2/4: Make meanings"


def raised(error: Exception) -> Exception:
    try:
        raise error
    except Exception as e:
        return e


class FailedStepOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.shown = mock.patch.object(step_failure, "show_exception").start()
        self.pane = mock.patch.object(step_failure, "report_run_error").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(api.take_stop_reason)

    def test_the_pane_takes_it_when_a_dialog_is_open(self):
        self.pane.return_value = True
        outcome = step_failure.failed_step_outcome(PARENT, raised(ValueError("boom")), TITLE)
        self.shown.assert_not_called()
        [(title, text)] = [c.args for c in self.pane.call_args_list]
        self.assertEqual(title, TITLE)
        # The message first, then the traceback the error box would have had
        self.assertTrue(text.startswith("ValueError: boom\n\nTraceback"), text)
        self.assertIn("in raised", text)
        self.assertEqual(outcome.status, chain_types.STEP_FAILED)
        self.assertEqual(outcome.error, "ValueError: boom")

    def test_aqts_error_box_takes_it_when_the_pane_cannot(self):
        self.pane.return_value = False
        error = raised(ValueError("boom"))
        outcome = step_failure.failed_step_outcome(PARENT, error, TITLE)
        self.shown.assert_called_once_with(parent=PARENT, exception=error)
        self.assertEqual(outcome.status, chain_types.STEP_FAILED)

    def test_an_error_never_raised_has_no_traceback_to_show(self):
        self.pane.return_value = True
        step_failure.failed_step_outcome(PARENT, KeyError("x"), TITLE)
        self.assertEqual(self.pane.call_args.args, (TITLE, "KeyError: 'x'"))

    def test_the_context_says_what_the_step_was_doing(self):
        self.pane.return_value = True
        outcome = step_failure.failed_step_outcome(
            PARENT, OSError(), TITLE, context="downloading the word array resources"
        )
        # A message-less exception is named by its type alone
        self.assertEqual(outcome.error, "OSError (while downloading the word array resources)")

    def test_a_broken_error_box_still_returns_the_outcome(self):
        self.pane.return_value = False
        self.shown.side_effect = RuntimeError("box broke")
        outcome = step_failure.failed_step_outcome(PARENT, ValueError("boom"), TITLE)
        self.assertEqual(outcome.status, chain_types.STEP_FAILED)

    def test_interrupted_is_shown_nowhere_but_still_fails_the_step(self):
        """aqt's own error box skips Interrupted; the pane must not show it either."""

        class Interrupted(Exception):
            pass

        with mock.patch.object(step_failure, "Interrupted", Interrupted):
            outcome = step_failure.failed_step_outcome(PARENT, raised(Interrupted()), TITLE)

        self.pane.assert_not_called()
        self.shown.assert_not_called()
        self.assertEqual(outcome.status, chain_types.STEP_FAILED)

    def test_the_stop_reason_is_taken(self):
        api._stop_reason = "the login expired"
        outcome = step_failure.failed_step_outcome(PARENT, ValueError("boom"), TITLE)
        self.assertEqual(outcome.stop_reason, "the login expired")
        self.assertIsNone(api.take_stop_reason())


class ChainStepTitleTests(unittest.TestCase):
    def test_the_title_names_the_op_after_the_step(self):
        step = chain_types.ChainStep("Step 2/4", lambda _: None, "Make meanings")
        self.assertEqual(step.title, "Step 2/4: Make meanings")

    def test_without_an_op_label_the_title_is_the_step(self):
        self.assertEqual(chain_types.ChainStep("Step 1/1", lambda _: None).title, "Step 1/1")


if __name__ == "__main__":
    unittest.main()
