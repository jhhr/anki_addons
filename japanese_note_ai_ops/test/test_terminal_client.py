"""Tests for async_api_ops/terminal_client.py: the `terminal-` provider running `claude -p`.

No process is started: a fake Popen serves scripted results, shaped like the CLI's JSON output
recorded in task 14 (success, unknown model) plus synthetic rate-limit and usage-limit ones.
"""

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from addon_modules import FakeClock, PausingClock, load_ops_module

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
USAGE_LIMIT_6PM = cli_json(
    is_error=True, api_error_status=429, result="You've hit your session limit · resets 6pm"
)
# The whole of add_note_20260922_222819.log: 2018 requests, every one of them this
EXPIRED_LOGIN = cli_json(
    is_error=True,
    api_error_status=None,
    result="Failed to authenticate: OAuth session expired and could not be refreshed",
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
        if callable(self.outcome) and not self.killed:
            # Runs while the process is "running", then keeps it running
            self.outcome(self)
            raise subprocess.TimeoutExpired("claude", timeout)
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


def local(hour, minute, second=0, day=23):
    """A time.time() value for a local time of day, so the tests hold in any timezone."""
    return datetime(2026, 9, day, hour, minute, second).timestamp()


class CountingSemaphore:
    """Stands in for the process semaphore; `on_acquire(n)` runs as the n-th caller gets a slot."""

    def __init__(self, on_acquire=lambda n: None):
        self.on_acquire = on_acquire
        self.acquired = 0
        self.released = 0

    def acquire(self, timeout=None):
        self.acquired += 1
        self.on_acquire(self.acquired)
        return True

    def release(self):
        self.released += 1


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
            ((1, EXPIRED_LOGIN), tc.CliAction.UNAUTHENTICATED),
            ((1, cli_json(is_error=True, api_error_status=401)), tc.CliAction.UNAUTHENTICATED),
            (
                (1, cli_json(is_error=True, result="Invalid API key · Please run /login")),
                tc.CliAction.UNAUTHENTICATED,
            ),
            ((3221225786, ""), tc.CliAction.RETRY),
        ]
        for (exit_code, stdout), action in cases:
            with self.subTest(stdout=stdout[:60]):
                self.assertEqual(tc.classify_result(exit_code, stdout, "").action, action)
        # A login failure the CLI printed instead of returning is still a dead end, not a crash
        self.assertEqual(
            tc.classify_result(1, "", "Authentication failed").action, tc.CliAction.UNAUTHENTICATED
        )
        self.assertEqual(tc.classify_result(0, SUCCESS, "").result, {"decision": "match"})
        self.assertEqual(
            tc.classify_result(1, USAGE_LIMIT, "").message, json.loads(USAGE_LIMIT)["result"]
        )

    def test_decode_text_result(self):
        self.assertEqual(tc.decode_text_result('x {"a": 1} y'), {"a": 1})
        fixed = tc.decode_text_result('{"a": 1,}', lambda s: s.replace(",}", "}"))
        self.assertEqual(fixed, {"a": 1})
        self.assertIsNone(tc.decode_text_result("no json"))

    def test_second_guessed_answer_takes_the_last_object(self):
        # Shape of the terminal Opus/Haiku answers in output/kanjify_eval_21e_stderr.txt
        text = (
            '```json\n{\n  "kanjified_sentence": "<k> 真事[マジック]</k>"\n}\n```\n\n'
            'Wait, let me reconsider. {not json} "マジック" is a loanword.\n\n'
            '```json\n{\n  "kanjified_sentence": "マジック"\n}\n```\n\nThis is correct.'
        )
        self.assertEqual(tc.decode_text_result(text), {"kanjified_sentence": "マジック"})
        nested = '{"a": {"b": 1}}\n\nWait — correction below.\n\n{"a": {"b": 2}}'
        self.assertEqual(tc.decode_text_result(nested), {"a": {"b": 2}})

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


class UsageLimitResumeTimeTests(unittest.TestCase):
    def test_reset_times(self):
        now = local(11, 0)
        tomorrow = datetime(2026, 9, 24, 9, 0).timestamp()
        cases = [
            ("You've hit your session limit · resets 5pm", local(17, 0)),
            ("You've hit your session limit · resets 5:30pm", local(17, 30)),
            ("Usage limit reached, resets at 5 pm", local(17, 0)),
            ("Your limit will reset at 17:00", local(17, 0)),
            ("You've hit your limit · resets 5pm (Europe/Helsinki)", local(17, 0)),
            ("resets 12pm", local(12, 0)),
            ("Resets 9 A.M.", tomorrow),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(tc.usage_limit_resume_time(message, now), expected)

    def test_a_time_already_passed_today_is_tomorrow(self):
        tomorrow = datetime(2026, 9, 24, 17, 0).timestamp()
        self.assertEqual(tc.usage_limit_resume_time("resets 5pm", local(17, 0, 30)), tomorrow)
        # The reset instant itself has passed too: the limit is not reset "now" but tomorrow
        self.assertEqual(tc.usage_limit_resume_time("resets 5pm", local(17, 0)), tomorrow)
        midnight = datetime(2026, 9, 24, 0, 0).timestamp()
        self.assertEqual(tc.usage_limit_resume_time("resets 12am", local(23, 0)), midnight)

    def test_no_time_is_none(self):
        for message in (
            "You've hit your session limit",
            "resets 5",
            "resets in 5 hours",
            "resets 13pm",
            "resets 25:00",
            "resets 5:75pm",
            "",
        ):
            with self.subTest(message=message):
                self.assertIsNone(tc.usage_limit_resume_time(message, local(11, 0)))


class PauseForUsageLimitTests(unittest.TestCase):
    def setUp(self):
        api.begin_run()

    def tearDown(self):
        api.end_run()
        api.take_stop_reason()

    def test_pauses_until_the_stated_reset(self):
        now = local(16, 0)
        self.assertTrue(tc.pause_for_usage_limit(" hit your limit · resets 5pm ", {}, now))
        pause = api.pause_state()
        self.assertEqual(pause.resume_at, local(17, 0))
        self.assertTrue(pause.automatic)
        self.assertEqual(pause.reason, "usage limit was reached: hit your limit · resets 5pm")
        self.assertFalse(api.run_cancelled())

    def test_the_first_pause_stands(self):
        now = local(16, 0)
        self.assertTrue(tc.pause_for_usage_limit("resets 5pm", {}, now))
        # Another worker's request hit the limit too, and reads a later time off its message
        self.assertTrue(tc.pause_for_usage_limit("resets 6pm", {}, now + 30))
        self.assertEqual(api.pause_state().resume_at, local(17, 0))
        self.assertIn("resets 5pm", api.pause_state().reason)

    def test_no_reset_time_falls_back_to_the_configured_interval(self):
        now = local(16, 0)
        tc.pause_for_usage_limit("You've hit your limit", {}, now)
        self.assertEqual(api.pause_state().resume_at, now + 15 * 60)
        api.resume_run()
        config = {"terminal_usage_limit_retry_minutes": 30}
        tc.pause_for_usage_limit("You've hit your limit", config, now)
        self.assertEqual(api.pause_state().resume_at, now + 30 * 60)

    def test_a_reset_a_day_away_is_not_believed(self):
        # A request in flight across the reset comes back limited just after it; "5pm" then
        # reads as tomorrow, and the run would sit out a whole day
        now = local(17, 0, 30)
        tc.pause_for_usage_limit("resets 5pm", {}, now)
        self.assertEqual(api.pause_state().resume_at, now + 15 * 60)
        api.resume_run()
        # Tomorrow within the limit is believed
        tc.pause_for_usage_limit("resets 1am", {}, local(23, 0))
        self.assertEqual(api.pause_state().resume_at, datetime(2026, 9, 24, 1, 0).timestamp())

    def test_outside_a_run_nothing_is_paused(self):
        api.end_run()
        self.assertFalse(tc.pause_for_usage_limit("resets 5pm", {}, local(16, 0)))
        self.assertIsNone(api.pause_state())


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

    def test_second_guessed_answer_is_not_retried(self):
        text = '{"decision": "match"}\n\nWait — correction below.\n\n{"decision": "dontmatch"}'
        popen = FakePopen((0, cli_json(result=text)))
        self.assertEqual(self.ask(popen), {"decision": "dontmatch"})
        self.assertEqual(len(popen.calls), 1)

    def test_retry_then_success(self):
        popen = FakePopen((1, RATE_LIMITED), (0, "not json"), (0, SUCCESS))
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(len(popen.calls), 3)
        self.assertGreater(self.clock.total_slept, 0)

    def test_gives_up_after_retries(self):
        popen = FakePopen((1, RATE_LIMITED))
        self.assertIsNone(self.ask(popen))
        self.assertEqual(len(popen.calls), 3)

    def test_fail_and_dead_ends_not_retried(self):
        for outcome in ((1, BAD_MODEL), (1, USAGE_LIMIT), (1, EXPIRED_LOGIN)):
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

    def test_cancel_run_kills_live_processes(self):
        api.begin_run()
        self.addCleanup(api.end_run)
        seen = {}

        def cancel_while_running(proc):
            seen["registered"] = proc in tc._live_processes
            api.cancel_run()

        popen = FakePopen(cancel_while_running)
        self.assertIsNone(self.ask(popen))
        self.assertTrue(seen["registered"])
        self.assertTrue(popen.processes[0].killed)
        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(tc._live_processes, set())

    def test_usage_limit_pauses_the_run_and_retries_the_request(self):
        popen = FakePopen((1, USAGE_LIMIT), (0, SUCCESS))
        seen = []

        def on_sleep(_sleeps):
            seen.append((len(popen.calls), api.pause_state()))

        clock = self.paused_run(on_sleep, start=local(16, 59))
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(len(popen.calls), 2)
        # Held for the minute up to the reset, spawning nothing meanwhile
        self.assertEqual(clock.total_slept, 60)
        self.assertEqual({calls for calls, _ in seen}, {1})
        pause = seen[0][1]
        self.assertEqual(pause.resume_at, local(17, 0))
        self.assertTrue(pause.automatic)
        self.assertIn("resets 5pm", pause.reason)
        self.assertIsNone(api.pause_state())
        self.assertFalse(api.run_cancelled())
        self.assertIsNone(api.take_stop_reason())

    def test_expired_login_stops_the_run(self):
        api.begin_run()
        self.addCleanup(api.end_run)
        self.assertIsNone(self.ask(FakePopen((1, EXPIRED_LOGIN))))
        self.assertTrue(api.run_cancelled())
        reason = api.take_stop_reason()
        self.assertIn("login has expired", reason)
        self.assertIn("OAuth session expired", reason)

        later = FakePopen((0, SUCCESS))
        self.assertIsNone(self.ask(later))
        self.assertEqual(later.calls, [])

    def test_dead_ends_outside_a_run_only_fail(self):
        for outcome in ((1, USAGE_LIMIT), (1, EXPIRED_LOGIN)):
            with self.subTest(outcome=outcome[1][:40]):
                self.assertIsNone(self.ask(FakePopen(outcome)))
                self.assertFalse(api.run_cancelled())
                self.assertIsNone(api.take_stop_reason())
                self.assertEqual(self.ask(FakePopen((0, SUCCESS))), {"decision": "match"})

    def paused_run(self, on_sleep, start: float = 1000.0) -> PausingClock:
        """A run on this thread, paused, with the clock driving (and ending) the wait."""
        api.begin_run()
        self.addCleanup(api.end_run)
        self.addCleanup(api.take_stop_reason)
        clock = PausingClock(on_sleep, start=start)
        # tearDown puts both clocks back
        setattr(tc, "time", clock)
        setattr(api, "time", clock)
        return clock

    def test_a_paused_run_spawns_nothing_until_resumed(self):
        popen = FakePopen((0, SUCCESS))
        spawned_while_paused = []

        def on_sleep(sleeps):
            spawned_while_paused.append(len(popen.calls))
            if sleeps == 3:
                api.resume_run()

        self.paused_run(on_sleep)
        api.pause_run("paused by user")
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(spawned_while_paused, [0, 0, 0])
        self.assertEqual(len(popen.calls), 1)

    def test_a_retry_waits_for_resume(self):
        """Paused during the backoff after a rate limit: the retry is held past the backoff."""
        popen = FakePopen((1, RATE_LIMITED), (0, SUCCESS))
        spawned = []

        def on_sleep(sleeps):
            spawned.append(len(popen.calls))
            if sleeps == 1:
                api.pause_run("paused by user")
            if sleeps == 20:
                api.resume_run()

        clock = self.paused_run(on_sleep)
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(spawned, [1] * 20)
        self.assertEqual(len(clock.slept), 20)
        self.assertEqual(len(popen.calls), 2)

    def test_a_cancel_while_paused_spawns_nothing(self):
        popen = FakePopen((0, SUCCESS))
        cancel_state = FakeCancelState()

        def on_sleep(sleeps):
            if sleeps == 2:
                cancel_state.cancelled = True

        self.paused_run(on_sleep)
        api.pause_run("paused by user")
        self.assertIsNone(self.ask(popen, cancel_state=cancel_state))
        self.assertEqual(popen.calls, [])

    def test_a_usage_limit_spends_no_retry(self):
        popen = FakePopen((1, USAGE_LIMIT), (1, RATE_LIMITED))
        self.paused_run(lambda _sleeps: None, start=local(16, 59))
        self.assertIsNone(self.ask(popen, {"max_request_retries": 2}))
        # The retried request still gets its first attempt and both retries
        self.assertEqual(len(popen.calls), 1 + 3)

    def test_every_request_that_hit_the_limit_waits_out_the_first_pause(self):
        inner = FakePopen((1, USAGE_LIMIT_6PM), (0, SUCCESS))

        def popen(cmd, **kwargs):
            if not inner.calls:
                # Another worker's request hits the limit while this one is in flight
                self.assertTrue(tc.pause_for_usage_limit(json.loads(USAGE_LIMIT)["result"], {}))
            return inner(cmd, **kwargs)

        clock = self.paused_run(lambda _sleeps: None, start=local(16, 59))
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(len(inner.calls), 2)
        # Retried at 5pm, when the first pause ended, not at the 6pm its own message gave
        self.assertEqual(clock.now, local(17, 0))
        self.assertFalse(api.run_cancelled())

    def test_a_limit_hit_again_after_the_reset_pauses_again(self):
        popen = FakePopen((1, USAGE_LIMIT), (1, USAGE_LIMIT), (0, SUCCESS))
        pauses = []

        def on_sleep(_sleeps):
            pause = api.pause_state()
            if not pauses or pauses[-1] is not pause:
                pauses.append(pause)

        clock = self.paused_run(on_sleep, start=local(16, 59))
        config = {"max_request_retries": 2, "terminal_usage_limit_retry_minutes": 1}
        self.assertEqual(self.ask(popen, config), {"decision": "match"})
        self.assertEqual(len(popen.calls), 3)
        # The second "resets 5pm" arrives at 17:00, reads as 5pm tomorrow, and is not believed
        self.assertEqual([p.resume_at for p in pauses], [local(17, 0), local(17, 1)])
        self.assertEqual(clock.now, local(17, 1))

    def test_a_cancel_during_the_usage_limit_pause_spawns_nothing(self):
        popen = FakePopen((1, USAGE_LIMIT), (0, SUCCESS))

        def on_sleep(sleeps):
            if sleeps == 3:
                api.cancel_run()

        self.paused_run(on_sleep, start=local(16, 59))
        self.assertIsNone(self.ask(popen))
        self.assertEqual(len(popen.calls), 1)
        self.assertTrue(api.run_cancelled())
        reason = api.take_stop_reason()
        self.assertIn("cancelled while paused: usage limit was reached", reason)
        self.assertIn("resets 5pm", reason)

    def use_semaphore(self, semaphore: CountingSemaphore) -> None:
        """Serve `semaphore` as the process semaphore for the default config's size."""
        saved = (tc._semaphore, tc._semaphore_size)
        setattr(tc, "_semaphore", semaphore)
        setattr(tc, "_semaphore_size", tc.DEFAULT_MAX_CONCURRENT)

        def restore() -> None:
            setattr(tc, "_semaphore", saved[0])
            setattr(tc, "_semaphore_size", saved[1])

        self.addCleanup(restore)

    def test_a_pause_while_queued_for_a_process_slot_spawns_nothing(self):
        # The usage limit lands while this worker waits behind the full process semaphore
        semaphore = CountingSemaphore(
            lambda n: api.pause_run("usage limit", local(17, 0)) if n == 1 else None
        )
        self.use_semaphore(semaphore)
        popen = FakePopen((0, SUCCESS))
        spawned = []

        def on_sleep(_sleeps):
            spawned.append(len(popen.calls))

        clock = self.paused_run(on_sleep, start=local(16, 59))
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(set(spawned), {0})
        self.assertEqual(clock.now, local(17, 0))
        self.assertEqual(len(popen.calls), 1)
        # The slot was given back for the pause and taken again after it
        self.assertEqual((semaphore.acquired, semaphore.released), (2, 2))

    def test_a_pause_during_the_cooldown_waits_before_taking_a_slot(self):
        semaphore = CountingSemaphore()
        self.use_semaphore(semaphore)
        popen = FakePopen((0, SUCCESS))
        spawned = []

        def on_sleep(sleeps):
            spawned.append(len(popen.calls))
            if sleeps == 1:
                api.pause_run("paused by user")
            if sleeps == 10:
                api.resume_run()

        self.paused_run(on_sleep)
        # 2 s of cooldown is 4 slices; the pause lands in the first and lasts to the tenth
        api.rate_limit_tracker.note_rate_limited("terminal:claude-haiku-4-5", 2.0)
        self.assertEqual(self.ask(popen), {"decision": "match"})
        self.assertEqual(spawned, [0] * 10)
        self.assertEqual(semaphore.acquired, 1)

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
