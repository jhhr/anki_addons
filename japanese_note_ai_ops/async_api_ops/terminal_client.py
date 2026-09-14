"""Running AI requests through the `claude` command line instead of an HTTP API.

A model configured as `terminal-<model>` (e.g. `terminal-claude-haiku-4-5`) goes to
`claude -p` on the user's subscription. Each request is one process: the prompt on stdin, the
instructions as the system prompt, the response schema as `--json-schema`, and the JSON result
on stdout. Switching a job back to an API model is only a config edit.

Like every other provider this is called from a worker thread and blocks it. A process costs
far more than a connection - a couple of seconds of startup and a few hundred MB - so the number
running at once has its own cap, `terminal_max_concurrent_requests`, on top of the gate.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from .api_client import (
    CANCEL_POLL_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT_SECONDS,
    _backoff_delay,
    _sleep_cancellable,
    is_cancelled,
    rate_limit_tracker,
)

logger = logging.getLogger(__name__)

TERMINAL_PREFIX = "terminal-"
TERMINAL = "terminal"
DEFAULT_MAX_CONCURRENT = 16
DEFAULT_TIMEOUT = 180
# Windows caps a whole command line at 32767 characters; longer instructions go in a file
MAX_INLINE_INSTRUCTIONS = 8000
# The process runs here, so it never picks up a project's CLAUDE.md or settings from Anki's cwd
WORK_DIR = Path(__file__).resolve().parent.parent / "user_files" / "terminal_cwd"
# The API path sends no thinking; with it on Haiku takes twice as long per call
CLI_ENV = {"MAX_THINKING_TOKENS": "0"}
RETRY_STATUSES = frozenset({429, 529})
# Wording of the subscription usage limit, which no retry within a run can clear. Not seen in a
# real response yet, only in the CLI's strings: "You've hit your ... limit", "resets ...".
USAGE_LIMIT_RE = re.compile(r"hit your .*limit|usage limit|limit reached|limit will reset", re.I)


def is_terminal_model(model: str) -> bool:
    return model.startswith(TERMINAL_PREFIX)


def cli_model(model: str) -> str:
    """The model the CLI is told to use: "terminal-claude-haiku-4-5" -> "claude-haiku-4-5"."""
    return model[len(TERMINAL_PREFIX) :] if is_terminal_model(model) else model


def build_command(
    exe: str,
    model: str,
    instructions: Optional[str] = None,
    schema: Optional[dict] = None,
    instructions_file: Optional[str] = None,
) -> list[str]:
    """The argument list for one request; the prompt itself goes to stdin.

    `instructions_file` replaces `instructions` when they are too long for the command line.
    No tools, no session saved, and `--safe-mode` so no hooks, MCP servers or plugins load.
    """
    cmd = [exe, "-p", "--model", cli_model(model), "--output-format", "json", "--tools", ""]
    cmd += ["--no-session-persistence", "--safe-mode"]
    if instructions_file:
        cmd += ["--system-prompt-file", instructions_file]
    elif instructions:
        cmd += ["--system-prompt", instructions]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    return cmd


class CliAction:
    OK = "ok"
    RETRY = "retry"
    # The subscription's usage limit: nothing more will go through until it resets
    EXHAUSTED = "exhausted"
    FAIL = "fail"


class CliOutcome(NamedTuple):
    action: str
    # OK: the schema object when the CLI parsed one, else None and the answer is in `message`
    result: Optional[dict]
    # OK without a parsed object: the model's text. Otherwise what went wrong.
    message: str
    status: Optional[int] = None


def classify_result(exit_code: Optional[int], stdout: str, stderr: str) -> CliOutcome:
    """Decide what to do with a finished `claude -p --output-format json` process."""
    try:
        body = json.loads(stdout)
    except (ValueError, TypeError):
        body = None
    if not isinstance(body, dict):
        # A crash or a kill leaves no JSON: this process's problem, worth another try
        tail = (stderr or stdout or "").strip()[-500:]
        return CliOutcome(CliAction.RETRY, None, f"exit {exit_code}, no JSON output: {tail}")

    text = body.get("result")
    text = text if isinstance(text, str) else ""
    status = body.get("api_error_status")
    status = status if isinstance(status, int) else None
    if not body.get("is_error") and exit_code == 0:
        structured = body.get("structured_output")
        if isinstance(structured, dict):
            return CliOutcome(CliAction.OK, structured, text)
        return CliOutcome(CliAction.OK, None, text)

    if USAGE_LIMIT_RE.search(text):
        return CliOutcome(CliAction.EXHAUSTED, None, text, status)
    if status is not None and (status in RETRY_STATUSES or status >= 500):
        return CliOutcome(CliAction.RETRY, None, text, status)
    return CliOutcome(CliAction.FAIL, None, text or stdout.strip()[-500:], status)


def decode_text_result(text: str, corrector: Optional[Callable[[str], str]] = None):
    """The JSON object in a text answer, as the API providers read one: from the first "{" to
    the last "}", given to `corrector` once if it doesn't parse."""
    start, end = text.find("{"), text.rfind("}")
    json_text = text[start : end + 1] if start != -1 and end != -1 else text
    for attempt in (json_text, None):
        if attempt is None:
            if not corrector:
                break
            attempt = corrector(json_text)
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    logger.error("Failed to parse JSON from the claude CLI: %s", text)
    return None


def find_cli(config: dict, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """The claude executable: `claude_cli_path` if set, else the one on PATH.

    npm installs `claude` as .cmd/.ps1 shims; running those goes through a shell that mangles the
    schema argument's quotes and sits between us and the process we would kill. The native
    binary they launch is next to them, so that is used instead.
    """
    configured = str(config.get("claude_cli_path") or "").strip()
    if configured:
        return configured
    found = which("claude")
    if not found:
        return None
    path = Path(found)
    if path.suffix.lower() == ".exe":
        return found
    native = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    for name in ("claude.exe", "claude"):
        if (native / name).is_file():
            return str(native / name)
    return found if path.suffix.lower() not in (".cmd", ".ps1", ".bat") else None


# --- Running a process ---------------------------------------------------------------------

_semaphore_lock = threading.Lock()
_semaphore: Optional[threading.Semaphore] = None
_semaphore_size = 0


def process_semaphore(config: dict) -> threading.Semaphore:
    """The process-wide cap on running `claude` processes, sized from the config.

    Resized only by replacing it, so a config change takes effect for requests that start after
    it while the ones holding the old one finish normally.
    """
    global _semaphore, _semaphore_size
    size = max(1, int(config.get("terminal_max_concurrent_requests") or DEFAULT_MAX_CONCURRENT))
    with _semaphore_lock:
        if _semaphore is None or size != _semaphore_size:
            _semaphore = threading.Semaphore(size)
            _semaphore_size = size
        return _semaphore


def _acquire_cancellable(semaphore: threading.Semaphore, cancel_state: Optional[Any]) -> bool:
    while not semaphore.acquire(timeout=CANCEL_POLL_INTERVAL):
        if is_cancelled(cancel_state):
            return False
    if is_cancelled(cancel_state):
        semaphore.release()
        return False
    return True


class ProcessResult(NamedTuple):
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False


def run_process(
    cmd: list[str],
    prompt: str,
    timeout: float,
    cancel_state: Optional[Any] = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Optional[ProcessResult]:
    """Run one request's process to the end. None if the run was cancelled while it ran.

    The wait is sliced so a cancel is noticed within CANCEL_POLL_INTERVAL; a cancelled or timed
    out process is killed rather than left to finish in a thread nothing waits on.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(WORK_DIR),
        env={**os.environ, **CLI_ENV},
        **kwargs,
    )
    deadline = time.monotonic() + timeout
    data: Optional[bytes] = prompt.encode("utf-8")
    while True:
        try:
            # Input is only written on the first call; later calls keep collecting output
            out, err = proc.communicate(input=data, timeout=CANCEL_POLL_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            data = None
            cancelled = is_cancelled(cancel_state)
            if cancelled or time.monotonic() >= deadline:
                proc.kill()
                out, err = proc.communicate()
                if cancelled:
                    return None
                return ProcessResult(proc.returncode, _text(out), _text(err), timed_out=True)
    return ProcessResult(proc.returncode, _text(out), _text(err))


def _text(data: Optional[bytes]) -> str:
    return data.decode("utf-8", errors="replace") if data else ""


def get_response_from_terminal(
    model: str,
    prompt: str,
    config: dict,
    cancel_state: Optional[Any] = None,
    instructions: Optional[str] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_result_corrector: Optional[Callable[[str], str]] = None,
    popen: Callable[..., Any] = subprocess.Popen,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[dict]:
    """One request through `claude -p`, retried like post_with_retry retries an API call.

    Returns the parsed JSON object, or None after logging why. Temperature and the output token
    limit have no CLI flags and are ignored.
    """
    if is_cancelled(cancel_state):
        return None
    if temperature is not None or max_output_tokens is not None:
        logger.debug(
            "claude CLI ignores temperature %s / max_output_tokens %s",
            temperature,
            max_output_tokens,
        )
    exe = find_cli(config, which)
    if not exe:
        logger.error("No claude CLI found: install Claude Code or set claude_cli_path")
        return None

    instructions_file = None
    if instructions and len(instructions) > MAX_INLINE_INSTRUCTIONS:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(instructions)
            instructions_file = handle.name
    try:
        cmd = build_command(exe, model, instructions, response_schema, instructions_file)
        return _run_with_retry(
            model, cmd, prompt, config, cancel_state, json_result_corrector, popen
        )
    finally:
        if instructions_file:
            try:
                os.unlink(instructions_file)
            except OSError:
                pass


def _run_with_retry(
    model: str,
    cmd: list[str],
    prompt: str,
    config: dict,
    cancel_state: Optional[Any],
    json_result_corrector: Optional[Callable[[str], str]],
    popen: Callable[..., Any],
) -> Optional[dict]:
    key = f"{TERMINAL}:{cli_model(model)}"
    timeout = float(config.get("request_timeout") or DEFAULT_TIMEOUT)
    max_retries = int(config.get("max_request_retries", DEFAULT_MAX_RETRIES))
    max_retry_wait = float(config.get("max_retry_wait_seconds", DEFAULT_MAX_RETRY_WAIT_SECONDS))
    semaphore = process_semaphore(config)

    for attempt in range(max_retries + 1):
        if is_cancelled(cancel_state):
            logger.info("Skipping request to %s, the run was cancelled", key)
            return None
        cooldown = rate_limit_tracker.wait_time(key)
        if cooldown > 0 and not _sleep_cancellable(cooldown, cancel_state):
            return None
        if not _acquire_cancellable(semaphore, cancel_state):
            return None
        sent_at = time.monotonic()
        try:
            finished = run_process(cmd, prompt, timeout, cancel_state, popen)
        except OSError as e:
            logger.error("Could not start the claude CLI %s: %s", cmd[0], e)
            return None
        finally:
            semaphore.release()
        if finished is None or is_cancelled(cancel_state):
            logger.debug("Late response for %s discarded, operation was cancelled", key)
            return None

        if finished.timed_out:
            logger.warning("Request to %s timed out (attempt %d)", key, attempt + 1)
            outcome = CliOutcome(CliAction.RETRY, None, "timed out")
        else:
            outcome = classify_result(finished.exit_code, finished.stdout, finished.stderr)

        if outcome.action == CliAction.OK:
            rate_limit_tracker.note_success(key, sent_at=sent_at)
            if outcome.result is not None:
                return outcome.result
            return decode_text_result(outcome.message, json_result_corrector)
        if outcome.action == CliAction.EXHAUSTED:
            logger.error("claude CLI usage limit reached for %s: %s", key, outcome.message)
            return None
        if outcome.action == CliAction.FAIL:
            # Full output, so a failure this classifier doesn't know yet can be taught to it
            logger.error(
                "claude CLI request to %s failed: exit %s, stdout %s, stderr %s",
                key,
                finished.exit_code,
                finished.stdout,
                finished.stderr,
            )
            return None

        if attempt >= max_retries:
            logger.error("Giving up on %s after %d attempts: %s", key, attempt + 1, outcome.message)
            return None
        delay = min(_backoff_delay(attempt), max_retry_wait)
        logger.warning("Retrying %s in %.1fs: %s", key, delay, outcome.message)
        if outcome.status in RETRY_STATUSES:
            rate_limit_tracker.note_rate_limited(key, delay)
        if not _sleep_cancellable(delay, cancel_state):
            return None
    return None
