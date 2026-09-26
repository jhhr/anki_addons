"""Running AI requests through the `claude` command line instead of an HTTP API.

A model configured as `terminal-<model>` (e.g. `terminal-claude-haiku-4-5`) goes to
`claude -p` on the user's subscription. Each request is one process: the prompt on stdin, the
instructions as the system prompt, the response schema as `--json-schema`, and the JSON result
on stdout. Switching a job back to an API model is only a config edit.

Like every other provider this is called from a worker thread and blocks it. A process costs
far more than a connection - a couple of seconds of startup and a few hundred MB - so the number
running at once has its own cap, `terminal_max_concurrent_requests`, on top of the gate.

The subscription's usage limit pauses a bulk run until the reset time the CLI states, and every
request that hit the limit is retried after it; an expired login or a model the CLI cannot use,
which only the user can fix, stops the run. Outside a bulk run (an editor hook) any of them only
fails the request.
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from .api_client import (
    CANCEL_POLL_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT_SECONDS,
    _backoff_delay,
    _sleep_cancellable,
    add_cancel_hook,
    cancel_run,
    current_run,
    is_cancelled,
    pause_run,
    rate_limit_tracker,
    run_paused,
    wait_while_paused,
)
from .run_errors import excerpt, report_error

try:
    import psutil  # type: ignore
except ImportError:  # the addon's lib/ was not vendored; see build.py vendor
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

TERMINAL_PREFIX = "terminal-"
TERMINAL = "terminal"
DEFAULT_MAX_CONCURRENT = 16
DEFAULT_TIMEOUT = 180
# Windows caps a whole command line at 32767 characters; longer instructions go in a file
MAX_INLINE_INSTRUCTIONS = 8000
# The process runs here, so it never picks up a project's CLAUDE.md or settings from Anki's cwd.
# Not under the addon's user_files: an addon linked in from a git checkout resolves into that
# repo, and the CLI then runs git over the whole repo at every start, a fifth of its CPU.
WORK_DIR = Path(tempfile.gettempdir()) / "anki_claude_terminal_cwd"
CLI_ENV = {
    # The API path sends no thinking; with it on Haiku takes twice as long per call
    "MAX_THINKING_TOKENS": "0",
    # Telemetry, update checks and error reports each cost startup CPU in every process;
    # together they were about a quarter of a request's CPU
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
}
RETRY_STATUSES = frozenset({429, 529})
# Wording of the subscription usage limit, which no retry clears before its reset time, so the
# run pauses until then. Not seen in a real response yet, only in the CLI's strings: "You've
# hit your ... limit", "resets ...".
USAGE_LIMIT_RE = re.compile(r"hit your .*limit|usage limit|limit reached|limit will reset", re.I)
# The reset time in that message: "resets 5pm", "resets 5:30pm", "resets at 5 pm",
# "resets 17:00". A timezone after it, "(Europe/Helsinki)", is ignored: the CLI prints the
# user's own local time. A bare number ("resets 5") is not taken for an hour.
USAGE_LIMIT_RESET_RE = re.compile(
    r"\bresets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?m\.?)?(?![\w:])", re.I
)
DEFAULT_USAGE_LIMIT_RETRY_MINUTES = 15
# A reset time further ahead than this is not believed. It is read as the next occurrence of a
# time of day, so a request that was in flight across the reset and comes back limited just
# after it ("resets 5pm" received at 17:00:30) would otherwise pause the run until 5pm
# tomorrow. The session limit resets within hours; past this the fallback interval is used.
MAX_USAGE_LIMIT_PAUSE_SECONDS = 20 * 60 * 60
# Wording of an expired or missing login, which no retry can clear either: seen as
# "Failed to authenticate: OAuth session expired and could not be refreshed".
AUTH_FAILURE_RE = re.compile(
    r"failed to authenticate|authentication (?:failed|error)|oauth|invalid api key"
    r"|/login|not logged in|unauthorized",
    re.I,
)
AUTH_STATUSES = frozenset({401, 403})
# A model the CLI cannot use, which fails every request alike: a name it doesn't know, seen as
# a 404 "There's an issue with the selected model (claude-nope)", or one newer than the
# installed CLI, seen as a 400 "Claude Code 2.1.268 does not support this model; version
# 2.1.280 or newer is required. Run 'claude update'". Both print this tag on stderr.
UNUSABLE_MODEL_RE = re.compile(
    r"issue with the selected model|does not support this model|unrecognized_model", re.I
)


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
    # The subscription's usage limit: nothing more will go through until it resets, so a bulk
    # run pauses until then and the request is retried
    EXHAUSTED = "exhausted"
    # The CLI's login expired: nothing will go through until the user logs in again
    UNAUTHENTICATED = "unauthenticated"
    # The configured model is unknown to the CLI or needs a newer CLI
    UNUSABLE_MODEL = "unusable_model"
    FAIL = "fail"


# The dead ends: what went wrong for the whole run, not for this one request, and that no
# waiting will clear, so the run stops instead of spending its remaining notes on the same wall.
# The usage limit is not one: it clears at its reset time, and the run pauses until then.
STOP_REASONS = {
    CliAction.UNAUTHENTICATED: "login has expired - run `claude` in a terminal to log in again",
    CliAction.UNUSABLE_MODEL: (
        "cannot use the configured model - fix the operation's model name or run `claude update`"
    ),
}


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
        tail = (stderr or stdout or "").strip()[-500:]
        if AUTH_FAILURE_RE.search(tail):
            return CliOutcome(CliAction.UNAUTHENTICATED, None, tail)
        # A crash or a kill leaves no JSON: this process's problem, worth another try
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

    # Before the status checks: the 404 and 400 it comes as would otherwise fail only this
    # request, and a run of thousands failed every one of them before it was noticed
    if UNUSABLE_MODEL_RE.search(text) or UNUSABLE_MODEL_RE.search(stderr or ""):
        return CliOutcome(CliAction.UNUSABLE_MODEL, None, text or stderr.strip()[-500:], status)
    if USAGE_LIMIT_RE.search(text):
        return CliOutcome(CliAction.EXHAUSTED, None, text, status)
    if AUTH_FAILURE_RE.search(text) or status in AUTH_STATUSES:
        return CliOutcome(CliAction.UNAUTHENTICATED, None, text, status)
    if status is not None and (status in RETRY_STATUSES or status >= 500):
        return CliOutcome(CliAction.RETRY, None, text, status)
    return CliOutcome(CliAction.FAIL, None, text or stdout.strip()[-500:], status)


def last_json_object(text: str) -> Optional[dict]:
    """The last complete top-level JSON object in `text`, None if there is none.

    Claude in the terminal sometimes answers, writes "Wait, let me reconsider..." and answers
    again; the later object is the one it settled on.
    """
    decoder = json.JSONDecoder()
    found = None
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        pos = text.find("{", end)
    return found


def decode_text_result(text: str, corrector: Optional[Callable[[str], str]] = None):
    """The JSON object in a text answer: the last complete one, else read as the API providers
    read one, from the first "{" to the last "}", given to `corrector` once if it doesn't parse."""
    found = last_json_object(text)
    if found is not None:
        return found
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
    report_error(f"claude CLI: the answer was not valid JSON: {excerpt(text)}")
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


# Every `claude` process still running, so cancelling a run can kill them all at once instead of
# each worker noticing on its next poll
_live_lock = threading.Lock()
_live_processes: set = set()


def kill_process_tree(proc: Any) -> None:
    """Kill a process and whatever it started (claude.exe runs git, cmd and conhost children)."""
    pid = getattr(proc, "pid", None)
    if psutil is not None and isinstance(pid, int):
        try:
            children = psutil.Process(pid).children(recursive=True)
        except Exception:
            children = []
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
    try:
        proc.kill()
    except OSError:
        # Already exited
        pass


def kill_live_processes() -> int:
    """Kill every running `claude` process; api_client.cancel_run calls this."""
    with _live_lock:
        processes = list(_live_processes)
    for proc in processes:
        kill_process_tree(proc)
    return len(processes)


add_cancel_hook(kill_live_processes)


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
    with _live_lock:
        _live_processes.add(proc)
    try:
        return _wait_process(proc, prompt, timeout, cancel_state)
    finally:
        with _live_lock:
            _live_processes.discard(proc)


def _wait_process(
    proc: Any, prompt: str, timeout: float, cancel_state: Optional[Any]
) -> Optional[ProcessResult]:
    deadline = time.monotonic() + timeout
    data: Optional[bytes] = prompt.encode("utf-8")
    # A cancel between the spawn and the registry seeing the process would otherwise be missed
    if is_cancelled(cancel_state):
        kill_process_tree(proc)
    while True:
        try:
            # Input is only written on the first call; later calls keep collecting output
            out, err = proc.communicate(input=data, timeout=CANCEL_POLL_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            data = None
            cancelled = is_cancelled(cancel_state)
            if cancelled or time.monotonic() >= deadline:
                kill_process_tree(proc)
                out, err = proc.communicate()
                if cancelled:
                    return None
                return ProcessResult(proc.returncode, _text(out), _text(err), timed_out=True)
    if is_cancelled(cancel_state):
        return None
    return ProcessResult(proc.returncode, _text(out), _text(err))


def _text(data: Optional[bytes]) -> str:
    return data.decode("utf-8", errors="replace") if data else ""


def stop_run_for_dead_end(reason: str, message: str) -> None:
    """Cancel the run: every request after this one would hit the same wall.

    The processes still running are killed and nothing more is spawned; the op's end message
    says why. A request outside a bulk run (an editor hook) only fails.
    """
    if current_run() is None:
        return
    cancel_run(reason=f"The claude CLI {reason}: {message.strip()}")


def usage_limit_resume_time(message: str, now: float) -> Optional[float]:
    """When the usage limit `message` reports resets, as a time.time() value; None if unstated.

    The message gives a time of day and no date: it is read as local time today, or tomorrow
    if that time is not after `now`.
    """
    found = USAGE_LIMIT_RESET_RE.search(message)
    if not found:
        return None
    hour, minute = int(found.group(1)), int(found.group(2) or 0)
    meridiem = (found.group(3) or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif found.group(2) is None:
        return None
    if hour > 23 or minute > 59:
        return None
    reset = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset.timestamp() <= now:
        reset += timedelta(days=1)
    return reset.timestamp()


def pause_for_usage_limit(message: str, config: dict, now: Optional[float] = None) -> bool:
    """Pause the run until the usage limit `message` reports resets. False outside a run.

    Every request that hits the limit calls this. The first one's pause stands (pause_run keeps
    it) and the others only wait for it to end. When no reset time can be read, or none worth
    believing, the pause lasts `terminal_usage_limit_retry_minutes`, and the retry after it
    finds out whether the limit has cleared.
    """
    # This thread's own run rather than the one in progress: a thread outside the run is never
    # held by its pause, so it would retry into the limit at once, over and over
    if current_run() is None:
        return False
    if now is None:
        now = time.time()
    resume_at = usage_limit_resume_time(message, now)
    if resume_at is None or resume_at - now > MAX_USAGE_LIMIT_PAUSE_SECONDS:
        minutes = config.get("terminal_usage_limit_retry_minutes")
        # Never under a minute: a pause that ends on its first poll would send every waiting
        # request straight back into the limit, round and round
        resume_at = now + 60 * max(1.0, float(minutes or DEFAULT_USAGE_LIMIT_RETRY_MINUTES))
    message = message.strip()
    if pause_run(f"usage limit was reached: {message}", resume_at):
        logger.warning(
            "claude CLI usage limit was reached, pausing the run until %s: %s",
            datetime.fromtimestamp(resume_at).strftime("%H:%M"),
            message,
        )
    return True


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
        report_error("No claude CLI found: install Claude Code or set claude_cli_path")
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

    # Counted by hand rather than by a for loop: a request that hit the usage limit is retried
    # after the pause without spending an attempt, since nothing was wrong with it
    attempt = 0
    while True:
        if is_cancelled(cancel_state):
            logger.info("Skipping request to %s, the run was cancelled", key)
            return None
        # A paused run spawns nothing new, retries included; a cancel during the pause ends it
        if not wait_while_paused(cancel_state):
            return None
        cooldown = rate_limit_tracker.wait_time(key)
        if cooldown > 0:
            if not _sleep_cancellable(cooldown, cancel_state):
                return None
            # The run may have been paused while this request sat out the cooldown
            if not wait_while_paused(cancel_state):
                return None
        if not _acquire_cancellable(semaphore, cancel_state):
            return None
        if run_paused():
            # Paused while this request queued for a process slot. When the usage limit lands,
            # every worker queued behind the full semaphore is in here, and each would spawn a
            # process straight into the limit. Give the slot back and wait above.
            semaphore.release()
            continue
        sent_at = time.monotonic()
        try:
            finished = run_process(cmd, prompt, timeout, cancel_state, popen)
        except OSError as e:
            logger.error("Could not start the claude CLI %s: %s", cmd[0], e)
            report_error(f"Could not start the claude CLI {cmd[0]}: {e}")
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
            if not pause_for_usage_limit(outcome.message, config):
                logger.error("claude CLI usage limit was reached for %s: %s", key, outcome.message)
                report_error(f"{key}: the usage limit was reached: {excerpt(outcome.message)}")
                return None
            # The same attempt again once the pause ends, which the top of the loop waits for.
            # Every request that hit the limit retries, not only the one that paused the run:
            # they were all only early.
            continue
        if outcome.action in STOP_REASONS:
            reason = STOP_REASONS[outcome.action]
            logger.error("claude CLI %s, stopping %s: %s", reason, key, outcome.message)
            stop_run_for_dead_end(reason, outcome.message)
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
            report_error(
                f"{key}: the claude CLI failed with exit code {finished.exit_code}:"
                f" {excerpt(finished.stderr or finished.stdout or '')}"
            )
            return None

        if attempt >= max_retries:
            logger.error("Giving up on %s after %d attempts: %s", key, attempt + 1, outcome.message)
            report_error(f"{key}: gave up after {attempt + 1} attempts: {excerpt(outcome.message)}")
            return None
        delay = min(_backoff_delay(attempt), max_retry_wait)
        logger.warning("Retrying %s in %.1fs: %s", key, delay, outcome.message)
        if outcome.status in RETRY_STATUSES:
            rate_limit_tracker.note_rate_limited(key, delay)
        if not _sleep_cancellable(delay, cancel_state):
            return None
        attempt += 1
