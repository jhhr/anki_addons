"""One `claude -p` request from a triage script, on the subscription.

The addon's terminal client is built for bulk runs inside Anki: it turns thinking off
(`MAX_THINKING_TOKENS=0`) to keep Haiku fast, and pauses the run it belongs to at the usage
limit. The triage asks Opus for judgements where thinking pays, gives an effort level, and has no
run to pause, so it builds the same command with `--effort`, lets the model think, and waits out
a usage limit itself. It reuses the client's command line and its reading of the result.

Parallel requests go through `Pool`: at most `workers` processes, and none started while the
CPU is above `max_cpu` percent (8 processes pegged this 4-core PC and lagged it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Optional

from _bootstrap import load_root

terminal = load_root("async_api_ops.terminal_client")

MODEL = "claude-opus-5-5"
# What a Claude Code session puts in the environment of the processes it starts (the list is
# dev/headless.py's SESSION_ENV): a triage run started from a session must not hand them on
SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_PID",
)
ENV_EXTRA = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"}
LIMIT_WAIT_FALLBACK_S = 15 * 60

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # type: ignore[assignment]


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def environment() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
    env.update(ENV_EXTRA)
    return env


def ask(
    prompt: str,
    instructions: str,
    schema: Optional[dict],
    model: str = MODEL,
    effort: str = "",
    timeout: float = 1200,
    tools: str = "",
    retries: int = 3,
) -> tuple[Optional[dict], str, Optional[dict]]:
    """`(structured answer or None, the answer's text or what went wrong, usage)`. Waits out a
    usage limit and retries a crash; an expired login or an unusable model exits."""
    exe = shutil.which("claude")
    if exe is None:
        sys.exit("no `claude` on PATH")
    terminal.WORK_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8", dir=terminal.WORK_DIR
    ) as fh:
        fh.write(instructions)
        instructions_file = fh.name
    try:
        cmd = terminal.build_command(exe, model, schema=schema, instructions_file=instructions_file)
        if tools:
            cmd[cmd.index("--tools") + 1] = tools
        if effort:
            cmd += ["--effort", effort]
        attempt = 0
        while True:
            attempt += 1
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt.encode("utf-8"),
                    capture_output=True,
                    cwd=terminal.WORK_DIR,
                    env=environment(),
                    timeout=timeout,
                )
                outcome = terminal.classify_result(
                    proc.returncode, proc.stdout.decode("utf-8", "replace"),
                    proc.stderr.decode("utf-8", "replace"),
                )
            except subprocess.TimeoutExpired:
                outcome = terminal.CliOutcome(terminal.CliAction.RETRY, None, "timed out")
            if outcome.action == terminal.CliAction.OK:
                result = outcome.result
                if result is None:
                    result = terminal.last_json_object(outcome.message)
                return result, outcome.message, outcome.usage
            if outcome.action == terminal.CliAction.EXHAUSTED:
                resume = terminal.usage_limit_resume_time(outcome.message, time.time())
                wait = (resume - time.time()) if resume else LIMIT_WAIT_FALLBACK_S
                log(f"usage limit: {outcome.message.strip()[:200]}; waiting {wait / 60:.0f} min")
                time.sleep(max(60.0, wait + 60))
                continue
            if outcome.action in (terminal.CliAction.UNAUTHENTICATED,
                                  terminal.CliAction.UNUSABLE_MODEL):
                log(f"claude: {outcome.message.strip()[:300]}")
                os._exit(2)
            if attempt >= retries:
                return None, outcome.message, None
            log(f"retrying after: {outcome.message.strip()[:200]}")
            time.sleep(10 * attempt)
    finally:
        try:
            os.unlink(instructions_file)
        except OSError:
            pass


class Pool:
    """Runs jobs on at most `workers` threads, starting none while the CPU is busy."""

    def __init__(self, workers: int = 3, max_cpu: float = 85.0) -> None:
        self.workers = workers
        self.max_cpu = max_cpu
        self._start_lock = threading.Lock()

    def wait_for_cpu(self) -> None:
        if psutil is None:
            return
        with self._start_lock:
            while psutil.cpu_percent(interval=2.0) > self.max_cpu:
                time.sleep(5)

    def run(self, jobs: list, fn: Any) -> None:
        """`fn(job)` for each job; exceptions are logged and the job skipped."""
        queue = list(jobs)
        lock = threading.Lock()

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    job = queue.pop(0)
                self.wait_for_cpu()
                try:
                    fn(job)
                except Exception as e:  # noqa: BLE001 - one job's failure is not the run's
                    log(f"job failed: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(1.0)
