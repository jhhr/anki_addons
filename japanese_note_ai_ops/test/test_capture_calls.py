"""Capture of AI calls: `get_response` through each provider leaves one `calls` row with the
outcome, the answer before and after parsing, the usage and the number of sends.

The real request path runs. The HTTP providers answer from a fake session put in api_client's
session cache, so `post_with_retry` counts its sends; the claude CLI answers from a fake Popen,
so `classify_result` reads its JSON. A fake clock over api_client's and terminal_client's
`time` makes the retries instant. A store is installed in a temporary directory and shut down
in a cleanup, as in test_capture; rows are read after `flush()`.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional
from unittest import mock

import requests
from requests.structures import CaseInsensitiveDict

from addon_modules import FakeClock, load_ops_module

capture = load_ops_module("capture")
capture_store = load_ops_module("capture_store")
api = load_ops_module("api_client")
tc = load_ops_module("terminal_client")
run_errors = load_ops_module("run_errors")
base_ops = load_ops_module("base_ops")

API_KEY = "sk-capture-calls-test-0123456789abcdef"
CONFIG = {
    "google_api_key": API_KEY,
    "openai_api_key": API_KEY,
    "together_api_key": API_KEY,
    "anthropic_api_key": API_KEY,
    "max_request_retries": 1,
    "request_timeout": 5,
}
KIND = "match.meanings"
INPUTS = {"word": "食べる", "reading": "たべる", "sentence": "パンを食べる。"}
PROMPT = "Word: 食べる\nSentence: パンを食べる。"
INSTRUCTIONS = "語の意味を一つ選び、JSONで答えてください。"
SCHEMA = {
    "type": "object",
    "properties": {"meaning": {"type": "string"}},
    "additionalProperties": False,
}
PARAMS = {"max_output_tokens": 500, "temperature": 0.2, "effort": None}
# Fenced, so the text recorded as raw differs from the JSON parsed out of it
ANSWER_TEXT = '```json\n{"meaning": "to eat"}\n```'
ANSWER = {"meaning": "to eat"}
USAGE = {"input_tokens": 120, "output_tokens": 9}
TERMINAL_MODEL = "terminal-claude-haiku-4-5"


class FakeResponse:
    """Enough of requests.Response for classify_response and the providers (test_api_client)."""

    def __init__(self, status_code=200, headers=None, body=None, text=None):
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        if text is not None:
            self.text = text
        elif body is not None:
            self.text = json.dumps(body)
        else:
            self.text = ""


class FakeSession:
    """Serves scripted outcomes to post(): a response, an exception to raise, or a callable
    returning or raising one. The last outcome repeats (test_api_client)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - requests' name
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def cli_json(**fields) -> str:
    """The claude CLI's JSON output (test_terminal_client)."""
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


class FakeProcess:
    def __init__(self, outcome):
        self.outcome = outcome
        self.returncode = None

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen's name
        exit_code, stdout, *stderr = self.outcome
        self.returncode = exit_code
        return stdout.encode("utf-8"), "".join(stderr).encode("utf-8")

    def kill(self):
        pass


class FakePopen:
    """One scripted process per call; the last outcome repeats (test_terminal_client)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return FakeProcess(outcome)


def which(_name):
    return "C:/bin/claude.exe"


class Provider(NamedTuple):
    name: str
    model: str
    usage_key: str
    # The provider's answer envelope around a text
    envelope: Callable[[str], dict]
    # The system instructions in the request body it sent
    sent_instructions: Callable[[dict], str]


def _chat(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


PROVIDERS = [
    Provider(
        api.GEMINI,
        "gemini-2.5-flash",
        "usageMetadata",
        lambda text: {"candidates": [{"content": {"parts": [{"text": text}]}}]},
        lambda body: body["system_instruction"]["parts"][0]["text"],
    ),
    Provider(api.OPENAI, "gpt-4o", "usage", _chat, lambda body: body["messages"][0]["content"]),
    Provider(
        api.TOGETHER,
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "usage",
        _chat,
        lambda body: body["messages"][0]["content"],
    ),
    Provider(
        api.ANTHROPIC,
        "claude-haiku-4-5",
        "usage",
        lambda text: {"content": [{"type": "text", "text": text}]},
        lambda body: body["system"],
    ),
]


def answer(provider: Provider, text: str = ANSWER_TEXT, usage: Any = USAGE) -> FakeResponse:
    body = provider.envelope(text)
    if usage is not None:
        body[provider.usage_key] = usage
    return FakeResponse(200, body=body)


class Records(logging.Handler):
    """Keeps base_ops' records, each with the capture ids of where it was logged."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.addFilter(capture.CaptureContextFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class CallCaptureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(capture.current_store(), "a store was left installed")
        # Capture's warnings are rate limited per process (test_capture)
        capture._quiet_until.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(directory.name, "capture.sqlite3")

        self.config: Optional[dict] = dict(CONFIG)
        self.clock = FakeClock()
        manager = types.SimpleNamespace(getConfig=lambda _name: self.config)
        for target, name, value in (
            (base_ops.mw, "addonManager", manager),
            (api, "time", self.clock),
            (tc, "time", self.clock),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        sessions = dict(api._sessions)
        api._sessions.clear()
        self.addCleanup(self._restore_sessions, sessions)
        # A 429 leaves a cooldown on the model, timed by the fake clock
        api.rate_limit_tracker.reset()
        self.addCleanup(api.rate_limit_tracker.reset)

        self.reports: list[str] = []
        deliver = run_errors._deliver
        run_errors.deliver_with(lambda _title, text: self.reports.append(text))
        self.addCleanup(run_errors.deliver_with, deliver)

        self.log = Records()
        level = base_ops.logger.level
        base_ops.logger.setLevel(logging.INFO)
        base_ops.logger.addHandler(self.log)
        self.addCleanup(base_ops.logger.setLevel, level)
        self.addCleanup(base_ops.logger.removeHandler, self.log)

    @staticmethod
    def _restore_sessions(sessions) -> None:
        api._sessions.clear()
        api._sessions.update(sessions)

    def install(self) -> None:
        self.assertTrue(capture.install(self.path))
        self.addCleanup(capture.shutdown)

    def rows(self, table: str) -> list[dict]:
        self.assertTrue(capture.current_store().flush())
        key = {"runs": "run_id", "calls": "call_id", "blobs": "hash"}[table]
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY {key}")]

    def last_call(self) -> dict:
        return self.rows("calls")[-1]

    def blob(self, text_hash: Optional[str]) -> Optional[str]:
        return {row["hash"]: row["text"] for row in self.rows("blobs")}.get(text_hash)

    def serve(self, provider: Provider, *outcomes) -> FakeSession:
        session = FakeSession(*outcomes)
        api._sessions[provider.name] = session
        return session

    def ask(self, model: str, **kwargs):
        args: dict[str, Any] = dict(
            instructions=INSTRUCTIONS,
            response_schema=SCHEMA,
            max_output_tokens=500,
            temperature=0.2,
            kind=KIND,
            inputs=INPUTS,
        )
        args.update(kwargs)
        return base_ops.get_response(model, PROMPT, **args)

    def ask_terminal(self, popen, which=which, **kwargs):
        terminal = functools.partial(tc.get_response_from_terminal, popen=popen, which=which)
        with mock.patch.object(base_ops, "get_response_from_terminal", terminal):
            return self.ask(TERMINAL_MODEL, **kwargs)

    def reference_lines(self) -> list[logging.LogRecord]:
        return [r for r in self.log.records if r.getMessage().startswith("call ")]

    def assert_outcome(self, outcome: str, error: Optional[str] = None, attempts: int = 1):
        row = self.last_call()
        self.assertEqual(row["outcome"], outcome)
        if error is not None:
            self.assertEqual(row["error"], error)
        self.assertEqual(row["attempts"], attempts)
        return row


class HttpProviderTests(CallCaptureTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install()

    def test_an_answer_is_recorded_with_its_text_result_usage_and_keys(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                self.serve(provider, answer(provider))
                self.assertEqual(self.ask(provider.model), ANSWER)

                row = self.assert_outcome("ok", attempts=1)
                self.assertIsNone(row["error"])
                self.assertEqual(row["kind"], KIND)
                self.assertEqual(row["model"], provider.model)
                self.assertEqual(row["prompt"], PROMPT)
                self.assertEqual(row["response_raw"], ANSWER_TEXT)
                self.assertEqual(json.loads(row["response_json"]), ANSWER)
                self.assertEqual(json.loads(row["usage_json"]), USAGE)
                self.assertEqual(json.loads(row["params_json"]), PARAMS)
                self.assertEqual(json.loads(row["inputs_json"]), INPUTS)
                self.assertEqual(row["request_key"], capture_store.request_key(KIND, INPUTS))
                self.assertEqual(
                    row["prompt_key"],
                    capture_store.prompt_key(provider.model, INSTRUCTIONS, PROMPT, SCHEMA, PARAMS),
                )
                self.assertEqual(self.blob(row["instructions_hash"]), INSTRUCTIONS)
                self.assertEqual(json.loads(self.blob(row["schema_hash"])), SCHEMA)
                self.assertIsNone(row["extra_json"])

    def test_each_provider_sends_the_default_instructions_when_given_none_and_records_them(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                session = self.serve(provider, answer(provider))
                self.assertEqual(self.ask(provider.model, instructions=None), ANSWER)

                sent = provider.sent_instructions(session.calls[0]["json"])
                self.assertEqual(sent, base_ops.DEFAULT_SYSTEM_INSTRUCTION)
                self.assertEqual(self.blob(self.last_call()["instructions_hash"]), sent)

    def test_a_call_outside_a_run_is_its_own_implicit_run(self):
        provider = PROVIDERS[0]
        self.serve(provider, answer(provider), FakeResponse(400, text="bad"))
        self.ask(provider.model)
        self.ask(provider.model, kind="")

        first, second = self.rows("calls")
        runs = self.rows("runs")
        self.assertEqual([r["run_id"] for r in runs], [first["run_id"], second["run_id"]])
        self.assertEqual([r["implicit"] for r in runs], [1, 1])
        self.assertEqual([r["label"] for r in runs], [KIND, "call"])
        self.assertEqual([r["outcome"] for r in runs], ["ok", "refused"])

    def test_a_call_inside_a_run_joins_it(self):
        provider = PROVIDERS[1]
        self.serve(provider, answer(provider))
        run_id = capture.begin_run("Matching")
        with capture.run_scope(run_id):
            self.ask(provider.model)
        capture.end_run(run_id, "completed")

        self.assertEqual(self.last_call()["run_id"], run_id)
        self.assertEqual(len(self.rows("runs")), 1)

    def test_a_refused_request_keeps_the_provider_body(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                self.reports.clear()
                self.serve(provider, FakeResponse(400, text='{"error": "bad schema"}'))
                self.assertIsNone(self.ask(provider.model))

                row = self.assert_outcome("refused", 'HTTP 400: {"error": "bad schema"}')
                self.assertIsNone(row["response_raw"])
                self.assertIsNone(row["response_json"])
                self.assertEqual(len(self.reports), 1)

    def test_an_answer_whose_text_cannot_be_found_is_unreadable(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                self.serve(provider, FakeResponse(200, text='{"unexpected": 1}'))
                self.assertIsNone(self.ask(provider.model))

                row = self.assert_outcome("unreadable")
                self.assertTrue(row["error"].startswith("KeyError: "), row["error"])
                self.assertTrue(row["error"].endswith(': {"unexpected": 1}'), row["error"])

    def test_an_answer_that_is_not_json_is_unparseable_unless_the_corrector_fixes_it(self):
        def corrector(text: str) -> str:
            return text.replace(",}", "}")

        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                self.serve(provider, answer(provider, "no JSON here"))
                self.assertIsNone(self.ask(provider.model, json_result_corrector=corrector))
                row = self.assert_outcome("unparseable", "the answer was not valid JSON")
                self.assertEqual(row["response_raw"], "no JSON here")
                self.assertIsNone(row["extra_json"])

                self.serve(provider, answer(provider, '{"meaning": "x",}'))
                self.assertIsNone(self.ask(provider.model))
                self.assert_outcome("unparseable")

                self.serve(provider, answer(provider, '{"meaning": "x",}'))
                self.assertEqual(
                    self.ask(provider.model, json_result_corrector=corrector), {"meaning": "x"}
                )
                row = self.assert_outcome("ok")
                self.assertEqual(row["response_raw"], '{"meaning": "x",}')
                self.assertEqual(json.loads(row["extra_json"]), {"corrected": True})

                self.serve(provider, answer(provider))
                self.ask(provider.model, json_result_corrector=corrector)
                self.assertIsNone(self.last_call()["extra_json"])

    def test_a_rate_limited_send_and_its_retry_are_two_attempts(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.name):
                self.serve(
                    provider, FakeResponse(429, headers={"retry-after": "1"}), answer(provider)
                )
                self.assertEqual(self.ask(provider.model), ANSWER)
                self.assert_outcome("ok", attempts=2)

    def test_anthropic_s_temperature_retry_is_two_attempts_and_only_its_answer_counts(self):
        provider = PROVIDERS[3]
        rejected = FakeResponse(
            400, body={"error": {"message": "temperature is deprecated for this model"}}
        )
        session = self.serve(provider, rejected, answer(provider))
        self.assertEqual(self.ask(provider.model), ANSWER)

        self.assertNotIn("temperature", session.calls[1]["json"])
        row = self.assert_outcome("ok", attempts=2)
        self.assertIsNone(row["error"])
        self.assertEqual(self.reports, [])

    def test_a_cancelled_request_is_cancelled(self):
        provider = PROVIDERS[1]
        cancel_state = base_ops.CancelState()

        def cancel_while_in_flight():
            cancel_state.cancel()
            return answer(provider)

        self.serve(provider, cancel_while_in_flight)
        self.assertIsNone(self.ask(provider.model, cancel_state=cancel_state))
        self.assert_outcome("cancelled", attempts=1)

        # Cancelled before anything was sent
        self.assertIsNone(self.ask(provider.model, cancel_state=cancel_state))
        self.assert_outcome("cancelled", attempts=0)
        self.assertEqual(self.reports, [])

    def test_every_attempt_timing_out_is_no_response(self):
        provider = PROVIDERS[0]
        self.serve(provider, requests.exceptions.Timeout("read timed out"))
        self.assertIsNone(self.ask(provider.model))

        self.assert_outcome(
            "no_response", "every attempt timed out or lost its connection", attempts=2
        )
        self.assertEqual(len(self.reports), 1)

    def test_an_exception_in_a_provider_is_recorded_and_re_raised(self):
        provider = PROVIDERS[2]
        error = RuntimeError("provider broke")
        self.serve(provider, error)
        with self.assertRaises(RuntimeError) as caught:
            self.ask(provider.model)

        self.assertIs(caught.exception, error)
        self.assert_outcome("error", repr(error), attempts=1)
        [line] = self.reference_lines()
        self.assertTrue(line.getMessage().startswith(f"call {self.last_call()['call_id']} "))
        self.assertIn(f" {KIND} error ", line.getMessage())

    def test_an_unsupported_model_and_a_missing_config_are_errors(self):
        self.assertIsNone(self.ask("llama-3"))
        self.assert_outcome("error", "unsupported model: llama-3", attempts=0)

        self.config = None
        for model in [provider.model for provider in PROVIDERS] + [TERMINAL_MODEL]:
            with self.subTest(model=model):
                self.assertIsNone(self.ask(model))
                self.assert_outcome("error", "no configuration found for the addon", attempts=0)

    def test_the_reference_line_names_the_call_and_carries_its_ids(self):
        provider = PROVIDERS[0]
        self.serve(provider, answer(provider), FakeResponse(400, text="bad"))
        self.ask(provider.model)
        self.ask(provider.model, kind="")

        first, second = self.rows("calls")
        lines = self.reference_lines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0].getMessage(), rf"^call {first['call_id']} {KIND} ok \d+\.\ds$")
        self.assertRegex(lines[1].getMessage(), rf"^call {second['call_id']} - refused \d+\.\ds$")
        # Logged inside the call: its line is found by the call's id, and names the implicit run
        self.assertEqual(
            getattr(lines[0], "capture_ids"), f"r{first['run_id']} c{first['call_id']}"
        )

    def test_the_api_key_is_nowhere_in_the_database_file(self):
        for provider in PROVIDERS:
            self.serve(provider, answer(provider), FakeResponse(401, text="invalid key"))
            self.ask(provider.model)
            self.ask(provider.model)
        self.assertEqual(len(self.rows("calls")), 2 * len(PROVIDERS))
        self.assertTrue(capture.shutdown())

        files = [p for p in Path(self.directory).rglob("*") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(API_KEY.encode("utf-8"), path.read_bytes(), path.name)


class GeminiKeyTests(CallCaptureTestCase):
    """The Gemini key rides in a header. It was in the URL's query string, and a dropped
    connection's exception text carries the URL, which post_with_retry logs: every such log
    line held the key, in the files users are asked to share."""

    def test_the_key_is_sent_in_a_header_and_not_in_the_url(self):
        provider = PROVIDERS[0]
        session = self.serve(provider, answer(provider))
        self.assertEqual(self.ask(provider.model), ANSWER)

        [sent] = session.calls
        self.assertNotIn(API_KEY, sent["url"])
        self.assertTrue(sent["url"].endswith(f"/models/{provider.model}:generateContent"))
        self.assertEqual(sent["headers"]["x-goog-api-key"], API_KEY)

    def test_a_dropped_connection_logs_no_key(self):
        provider = PROVIDERS[0]

        class DroppingSession(FakeSession):
            def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
                self.calls.append({"url": url, "headers": headers})
                # What requests says: the URL, query string included, is in the text
                raise requests.exceptions.ConnectionError(
                    f"HTTPSConnectionPool(host='generativelanguage.googleapis.com', port=443):"
                    f" Max retries exceeded with url: {url}"
                )

        api._sessions[provider.name] = DroppingSession()
        records = Records()
        api.logger.addHandler(records)
        self.addCleanup(api.logger.removeHandler, records)
        level = api.logger.level
        api.logger.setLevel(logging.DEBUG)
        self.addCleanup(api.logger.setLevel, level)

        self.assertIsNone(self.ask(provider.model))

        messages = [r.getMessage() for r in records.records]
        self.assertTrue(any("Max retries exceeded" in m for m in messages), messages)
        for message in messages:
            self.assertNotIn(API_KEY, message)


class TerminalProviderTests(CallCaptureTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install()

    def test_an_answer_is_recorded_with_its_text_and_usage_and_cost(self):
        popen = FakePopen(
            (
                0,
                cli_json(
                    result=ANSWER_TEXT,
                    structured_output=ANSWER,
                    usage=USAGE,
                    total_cost_usd=0.0021,
                ),
            )
        )
        self.assertEqual(self.ask_terminal(popen), ANSWER)

        row = self.assert_outcome("ok", attempts=1)
        self.assertEqual(row["response_raw"], ANSWER_TEXT)
        self.assertEqual(json.loads(row["response_json"]), ANSWER)
        self.assertEqual(json.loads(row["usage_json"]), {**USAGE, "total_cost_usd": 0.0021})
        cmd = popen.calls[0]
        self.assertEqual(self.blob(row["instructions_hash"]), INSTRUCTIONS)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], INSTRUCTIONS)

    def test_the_default_instructions_are_sent_and_recorded(self):
        popen = FakePopen((0, cli_json(result=ANSWER_TEXT)))
        self.assertEqual(self.ask_terminal(popen, instructions=None), ANSWER)

        cmd = popen.calls[0]
        sent = cmd[cmd.index("--system-prompt") + 1]
        self.assertEqual(sent, base_ops.DEFAULT_SYSTEM_INSTRUCTION)
        self.assertEqual(self.blob(self.last_call()["instructions_hash"]), sent)

    def test_a_text_answer_unparseable_or_corrected(self):
        self.assertIsNone(self.ask_terminal(FakePopen((0, cli_json(result="no JSON here")))))
        row = self.assert_outcome("unparseable", "the answer was not valid JSON")
        self.assertEqual(row["response_raw"], "no JSON here")

        popen = FakePopen((0, cli_json(result='{"meaning": "x",}')))
        corrector = lambda text: text.replace(",}", "}")  # noqa: E731
        result = self.ask_terminal(popen, json_result_corrector=corrector)
        self.assertEqual(result, {"meaning": "x"})
        row = self.assert_outcome("ok")
        self.assertEqual(json.loads(row["extra_json"]), {"corrected": True})

    def test_a_retry_is_another_attempt_and_giving_up_is_no_response(self):
        rate_limited = cli_json(is_error=True, api_error_status=429, result="Rate limited")
        popen = FakePopen((1, rate_limited), (0, cli_json(result=ANSWER_TEXT)))
        self.assertEqual(self.ask_terminal(popen), ANSWER)
        self.assert_outcome("ok", attempts=2)

        self.assertIsNone(self.ask_terminal(FakePopen((1, rate_limited))))
        self.assert_outcome("no_response", "gave up after 2 attempts: Rate limited", attempts=2)

    def test_failures_are_errors(self):
        failed = cli_json(is_error=True, api_error_status=400, result="Bad request")
        bad_model = cli_json(
            is_error=True,
            api_error_status=404,
            result="There's an issue with the selected model (claude-nope).",
        )
        usage_limit = cli_json(
            is_error=True, api_error_status=429, result="You've hit your session limit"
        )

        def cannot_start(cmd, **kwargs):
            raise OSError("no such file")

        no_cli = lambda _name: None  # noqa: E731
        # (what, popen, which, the error's start, reports to the run's error list as before)
        cases = [
            ("CLI failure", FakePopen((1, failed)), which, "claude CLI failed with exit code 1", 1),
            # A dead end stops the run instead, which says why at its end
            ("dead end", FakePopen((1, bad_model)), which, "claude CLI cannot use the", 0),
            ("usage limit outside a run", FakePopen((1, usage_limit)), which, "usage limit", 1),
            ("no CLI", FakePopen((0, "")), no_cli, "no claude CLI found", 1),
            ("no process", cannot_start, which, "could not start the claude CLI", 1),
        ]
        for label, popen, find, error, reports in cases:
            with self.subTest(label):
                self.reports.clear()
                self.assertIsNone(self.ask_terminal(popen, which=find))
                row = self.last_call()
                self.assertEqual(row["outcome"], "error")
                self.assertTrue(row["error"].startswith(error), row["error"])
                self.assertEqual(len(self.reports), reports)

    def test_a_dead_end_that_stops_the_run_is_an_error_not_a_cancel(self):
        api.begin_run()
        self.addCleanup(api.end_run)
        self.addCleanup(api.take_stop_reason)
        expired = cli_json(is_error=True, result="Failed to authenticate: OAuth session expired")
        self.assertIsNone(self.ask_terminal(FakePopen((1, expired))))

        self.assertTrue(api.run_cancelled())
        row = self.assert_outcome("error")
        self.assertIn("login has expired", row["error"])


class NoStoreTests(CallCaptureTestCase):
    """Without a store, get_response sends and returns what it does with one, and nothing is
    written or logged of the call."""

    def exercise(self) -> list:
        seen: list = []

        def corrector(text: str) -> str:
            return text.replace(",}", "}")

        for provider in PROVIDERS:
            for outcome, kwargs in (
                (answer(provider), {}),
                (answer(provider), {"instructions": None, "kind": ""}),
                (FakeResponse(400, text="bad"), {}),
                (FakeResponse(200, text="{}"), {}),
                (answer(provider, '{"meaning": "x",}'), {"json_result_corrector": corrector}),
                (answer(provider, "no JSON"), {}),
            ):
                self.reports.clear()
                session = self.serve(provider, outcome)
                result = self.ask(provider.model, **kwargs)
                seen.append((provider.name, result, session.calls, list(self.reports)))
        popen = FakePopen((0, cli_json(result=ANSWER_TEXT, usage=USAGE)))
        seen.append(("terminal", self.ask_terminal(popen), popen.calls))
        return seen

    def test_get_response_behaves_the_same_with_and_without_a_store(self):
        without = self.exercise()
        self.assertEqual(os.listdir(self.directory), [])
        self.assertEqual(self.reference_lines(), [])

        self.install()
        self.assertEqual(self.exercise(), without)
        self.assertEqual(len(self.rows("calls")), len(without))


if __name__ == "__main__":
    unittest.main()
