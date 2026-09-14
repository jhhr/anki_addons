"""Tests for async_api_ops/terminal_client.py: the `terminal-` provider running `claude -p`.

No process is started: a fake Popen serves scripted results, shaped like the CLI's JSON output
recorded in task 14 (success, unknown model) plus synthetic rate-limit and usage-limit ones.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from addon_modules import FakeClock, load_ops_module  # type: ignore

tc = load_ops_module("terminal_client")
api = load_ops_module("api_client")

SCHEMA = {"type": "object", "properties": {"decision": {"type": "string"}}}


def cli_json(**fields) -> str:
    body = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "num_turns": 2,
        "result": "",
    }
    body.update(fields)
    return json.dumps(body)


SUCCESS = cli_json(result='{"decision": "match"}', structured_output={"decision": "match"})
TEXT_ONLY = cli_json(result='Here: {"decision": "dontmatch"}')
BAD_MODEL = cli_json(
    subtype="success",
    is_error=True,
    api_error_status=404,
    result="There's an issue with the selected model (claude-nope).",
)
RATE_LIMITED = cli_json(is_error=True, api_error_status=429, result="Rate limited")
USAGE_LIMIT = cli_json(
    is_error=True, api_error_status=429, result="You've hit your session limit · resets 5pm"
)


class FakeProcess:
    def __init__(self, outcome, clock=None):
        self.outcome = outcome
        self.clock = clock
        self.returncode = None
        self.inputs = []
        self.killed = False

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen's name
        self.inputs.append(input)
        if self.outcome == "hang" and not self.killed:
            self.clock.advance(timeout)
            raise subprocess.TimeoutExpired("claude", timeout)
        if self.killed:
            self.returncode = 1
            return b"", b""
        exit_code, stdout = self.outcome
        self.returncode = exit_code
        return stdout.encode("utf-8"), b""

    def kill(self):
        self.killed = True


class FakePopen:
    """Hands out one scripted process per call; the last outcome repeats."""

    def __init__(self, *outcomes, clock=None):
        self.outcomes = list(outcomes)
        self.clock = clock
        self.calls = []
        self.processes = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        proc = FakeProcess(outcome, self.clock)
        self.processes.append(proc)
        return proc


class FakeCancelState:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled

    def is_cancelled(self):
        return self.cancelled


def which(_name):
    return "C:/bin/claude.exe"


class ClockTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.saved = (tc.time, api.time)
        tc.time = api.time = self.clock
        api.rate_limit_tracker.reset()

    def tearDown(self):
        tc.time, api.time = self.saved
        api.rate_limit_tracker.reset()

    def ask(self, popen, config=None, **kwargs):
        return tc.get_response_from_terminal(
            "terminal-claude-haiku-4-5",
            "prompt 日本語",
            config or {"max_request_retries": 2},
            instructions="be brief",
            response_schema=SCHEMA,
            popen=popen,
            which=which,
            **kwargs,
        )


class PureTests(unittest.TestCase):
    def test_cli_model(self):
        self.assertTrue(tc.is_terminal_model("terminal-claude-haiku-4-5"))
        self.assertFalse(tc.is_terminal_model("claude-haiku-4-5"))
        self.assertEqual(tc.cli_model("terminal-claude-haiku-4-5"), "claude-haiku-4-5")

    def test_build_command(self):
        cmd = tc.build_command("claude.exe", "terminal-claude-haiku-4-5", "sys", SCHEMA)
        self.assertEqual(cmd[:4], ["claude.exe", "-p", "--model", "claude-haiku-4-5"])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--safe-mode", cmd)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "sys")
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), SCHEMA)

        cmd = tc.build_command("claude.exe", "m", "sys", None, instructions_file="f.md")
        self.assertEqual(cmd[cmd.index("--system-prompt-file") + 1], "f.md")
        self.assertNotIn("--system-prompt", cmd)
        self.assertNotIn("--json-schema", cmd)

    def test_classify_result(self):
        cases = [
            ((0, SUCCESS), tc.CliAction.OK),
            ((0, TEXT_ONLY), tc.CliAction.OK),
            ((1, BAD_MODEL), tc.CliAction.FAIL),
            ((1, RATE_LIMITED), tc.CliAction.RETRY),
            ((1, cli_json(is_error=True, api_error_status=529)), tc.CliAction.RETRY),
            ((1, USAGE_LIMIT), tc.CliAction.EXHAUSTED),
            ((3221225786, ""), tc.CliAction.RETRY),
        ]
        for (exit_code, stdout), action in cases:
            with self.subTest(stdout=stdout[:60]):
                self.assertEqual(tc.classify_result(exit_code, stdout, "").action, action)
        self.assertEqual(tc.classify_result(0, SUCCESS, "").result, {"decision": "match"})
        self.assertEqual(
            tc.classify_result(1, USAGE_LIMIT, "").message, json.loads(USAGE_LIMIT)["result"]
        )

    def test_decode_text_result(self):
        self.assertEqual(tc.decode_text_result('x {"a": 1} y'), {"a": 1})
        fixed = tc.decode_text_result('{"a": 1,}', lambda s: s.replace(",}", "}"))
        self.assertEqual(fixed, {"a": 1})
        self.assertIsNone(tc.decode_text_result("no json"))

    def test_find_cli(self):
        self.assertEqual(tc.find_cli({"claude_cli_path": "D:/c.exe"}, which), "D:/c.exe")
        self.assertIsNone(tc.find_cli({}, lambda _n: None))
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "claude.cmd"
            shim.write_text("")
            # A shim with no native binary beside it is not usable
            self.assertIsNone(tc.find_cli({}, lambda _n: str(shim)))
            native = Path(tmp) / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
            native.mkdir(parents=True)
            (native / "claude.exe").write_text("")
            self.assertEqual(tc.find_cli({}, lambda _n: str(shim)), str(native / "claude.exe"))

    def test_semaphore_follows_config(self):
        first = tc.process_semaphore({"terminal_max_concurrent_requests": 3})
        self.assertIs(tc.process_semaphore({"terminal_max_concurrent_requests": 3}), first)
        self.assertIsNot(tc.process_semaphore({}), first)
        self.assertEqual(tc._semaphore_size, tc.DEFAULT_MAX_CONCURRENT)


class RequestTests(ClockTestCase):
    def test_success_sends_prompt_on_stdin(self):
        popen = FakePopen((0, SUCCESS))
        self.assertEqual(self.ask(popen), {"decision": "match"})
        cmd, kwargs = popen.calls[0]
        self.assertEqual(cmd[0], "C:/bin/claude.exe")
        self.assertEqual(kwargs["env"]["MAX_THINKING_TOKENS"], "0")
        self.assertEqual(popen.processes[0].inputs[0], "prompt 日本語".encode("utf-8"))

    def test_text_answer_uses_corrector(self):
        popen = FakePopen((0, cli_json(result='{"decision": "x",}')))
        result = self.ask(popen, json_result_corrector=lambda s: s.replace(",}", "}"))
        self.assertEqual(result, {"decision": "x"})

    def test_retry_then_success(self):
        popen = FakePopen((1, RATE_LIMITED), (0, "not json"), (0, SUCCESS))
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(len(popen.calls), 3)
        self.assertGreater(self.clock.total_slept, 0)

    def test_gives_up_after_retries(self):
        popen = FakePopen((1, RATE_LIMITED))
        self.assertIsNone(self.ask(popen))
        self.assertEqual(len(popen.calls), 3)

    def test_fail_and_usage_limit_not_retried(self):
        for outcome in ((1, BAD_MODEL), (1, USAGE_LIMIT)):
            with self.subTest(outcome=outcome[1][:40]):
                popen = FakePopen(outcome)
                self.assertIsNone(self.ask(popen))
                self.assertEqual(len(popen.calls), 1)

    def test_timeout_kills_and_retries(self):
        popen = FakePopen("hang", (0, SUCCESS), clock=self.clock)
        config = {"max_request_retries": 1, "request_timeout": 2}
        self.assertEqual(self.ask(popen, config), {"decision": "match"})
        self.assertTrue(popen.processes[0].killed)
        self.assertEqual(len(popen.calls), 2)

    def test_cancelled_spawns_nothing(self):
        popen = FakePopen((0, SUCCESS))
        self.assertIsNone(self.ask(popen, cancel_state=FakeCancelState(True)))
        self.assertEqual(popen.calls, [])

    def test_long_instructions_go_in_a_file(self):
        seen = {}

        def popen(cmd, **kwargs):
            path = cmd[cmd.index("--system-prompt-file") + 1]
            seen["text"] = Path(path).read_text(encoding="utf-8")
            seen["path"] = path
            return FakeProcess((0, SUCCESS))

        long_text = "規則 " * tc.MAX_INLINE_INSTRUCTIONS
        result = tc.get_response_from_terminal(
            "terminal-claude-haiku-4-5", "p", {}, instructions=long_text, popen=popen, which=which
        )
        self.assertEqual(result, {"decision": "match"})
        self.assertEqual(seen["text"], long_text)
        self.assertFalse(Path(seen["path"]).exists())


if __name__ == "__main__":
    unittest.main()
